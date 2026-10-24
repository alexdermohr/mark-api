"""Narrow UID-bound HTTP transport for a separately owned Mark dashboard.

This is not a Chrome/CDP proxy or a security boundary by itself.  The trusted
backend must run the existing typed, witnessed Write API, own its SQLite state,
and be the only process with Chrome/CDP reachability.  The untrusted frontend
is a byte-forwarder with no data, browser, witness or operator mounts.
"""
from __future__ import annotations

from .container_draft import container_draft_denied

import argparse
from contextlib import closing
import errno
import os
import sys
from pathlib import Path
import select
import signal
import socket
import socketserver
import stat
import struct
from threading import BoundedSemaphore, Condition, Lock, Thread, Event
import time

from .fence_witness import _private_directory, FenceWitnessError


class DashboardRelayError(RuntimeError):
    """A transport boundary could not be established without ambiguity."""


_MAX_PEERS = 16
_SOCKET_TIMEOUT = 60.0
_IDLE_TIMEOUT = 90.0
_TOTAL_TIMEOUT = 300.0


def _peer_uid(sock: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise DashboardRelayError("kernel UNIX peer credentials are required")
    try:
        credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if len(credentials) != 12:
            raise DashboardRelayError("incomplete peer credentials")
        _pid, uid, _gid = struct.unpack("=iII", credentials)
        return uid
    except OSError as exc:
        raise DashboardRelayError("peer identity unavailable") from exc


def _chrome_cdp_reachable() -> bool:
    """Fail closed if the frontend can reach host CDP or trusted owner ports.

    Loopback alone is insufficient inside a Docker bridge namespace: a host
    service bound to 0.0.0.0 remains accessible via the bridge gateway.
    This is only an additional startup check. A root-attested host firewall
    and loopback-only CDP bind are mandatory before production release.
    """
    addresses = {"127.0.0.1"}
    try:
        routes = Path("/proc/net/route").read_text(encoding="ascii").splitlines()
        gateways: set[str] = set()
        for line in routes[1:]:
            columns = line.split()
            if len(columns) < 8:
                raise ValueError("malformed IPv4 kernel route")
            if columns[1] != "00000000" or columns[7] != "00000000":
                continue
            if not int(columns[3], 16) & 0x2:  # RTF_GATEWAY
                raise ValueError("default route without gateway")
            gateway = socket.inet_ntoa(struct.pack("<I", int(columns[2], 16)))
            if gateway == "0.0.0.0":
                raise ValueError("unspecified gateway")
            gateways.add(gateway)
        if not gateways:
            return True
        addresses.update(gateways)
    except (OSError, ValueError, UnicodeError, struct.error, IndexError):
        # An unreadable or unexpected kernel route is never proof of isolation.
        return True

    for address in addresses:
        for port in (9222, 18875, 18876):
            try:
                with closing(socket.create_connection(
                    (address, port), timeout=0.15,
                )):
                    return True
            except OSError:
                continue
    return False


def _relay_streams(
    first: socket.socket, second: socket.socket, *, stop: Event | None = None,
) -> None:
    """Transparent HTTP/1.x stream, fixed peer only, never another destination.

    This forwarder neither transforms requests nor retries writes.  If the
    connection is lost during an external operation, the trusted backend's
    independently witnessed unknown-outcome contract remains authoritative.
    """
    first.settimeout(_SOCKET_TIMEOUT)
    second.settimeout(_SOCKET_TIMEOUT)
    alive = [first, second]
    last_activity = time.monotonic()
    deadline = last_activity + _TOTAL_TIMEOUT
    while alive:
        if stop is not None and stop.is_set():
            return
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            raise DashboardRelayError("relay lifetime exceeded")
        idle_remaining = _IDLE_TIMEOUT - (now - last_activity)
        if idle_remaining <= 0:
            raise DashboardRelayError("relay idle timeout")
        readable, _, _ = select.select(
            alive, [], [], min(idle_remaining, remaining, .2),
        )
        if not readable:
            continue
        for source in readable:
            chunk = source.recv(65536)
            target = second if source is first else first
            if not chunk:
                alive.remove(source)
                try:
                    target.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            else:
                target.sendall(chunk)
                last_activity = time.monotonic()


class _LimitedThreading:
    """Limit accepted in-flight connections and make shutdown observable."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._slots = BoundedSemaphore(_MAX_PEERS)
        self._state = Condition(Lock())
        self._active = 0
        self._accepted: set[socket.socket] = set()
        self._stopping = Event()
        self._serving: Thread | None = None
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket.socket, client_address: object) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        with self._state:
            self._active += 1
            self._accepted.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._state:
                self._accepted.discard(request)
                self._active -= 1
                self._state.notify_all()
            self._slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._state:
                self._accepted.discard(request)
                self._active -= 1
                self._state.notify_all()
            self._slots.release()

    def start(self) -> None:
        if self._serving is not None:
            raise DashboardRelayError("relay already started")
        thread = Thread(target=self.serve_forever, kwargs={"poll_interval": 0.1},
                        daemon=True, name="mark-dashboard-relay")
        self._serving = thread
        thread.start()

    def stop(self, timeout_seconds: float = 25.0) -> bool:
        # Revoke accepted frontend streams before draining owned browser
        # operations. Closing an HTTP transport never authorizes resubmit;
        # the trusted backend keeps its witness/SQLite unknown fence.
        self._stopping.set()
        with self._state:
            connections = tuple(self._accepted)
        for sock in connections:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._serving is not None:
            if self._serving.is_alive():
                self.shutdown()
            self._serving.join(timeout=2)
        self.server_close()
        with self._state:
            return self._state.wait_for(
                lambda: self._active == 0, timeout=timeout_seconds,
            )


class _BackendHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        service: UnixDashboardRelay = self.server  # type: ignore[assignment]
        if _peer_uid(self.request) != service.frontend_uid:
            return
        try:
            with closing(socket.create_connection(
                ("127.0.0.1", service.dashboard_port), timeout=5.0,
            )) as dashboard:
                _relay_streams(self.request, dashboard, stop=service._stopping)
        except (OSError, TimeoutError, ValueError, DashboardRelayError):
            # An uncertain HTTP transport must never imply write success.
            return


class UnixDashboardRelay(
    _LimitedThreading, socketserver.ThreadingMixIn, socketserver.UnixStreamServer,
):
    """Owner-controlled socket; accepts only the configured frontend OS UID."""

    request_queue_size = _MAX_PEERS
    allow_reuse_address = False

    def __init__(
        self, *, socket_path: Path, frontend_uid: int, frontend_gid: int,
        dashboard_host: str, dashboard_port: int,
    ) -> None:
        if (
            not isinstance(socket_path, Path) or not socket_path.is_absolute()
            or type(frontend_uid) is not int or frontend_uid < 0
            or type(frontend_gid) is not int or frontend_gid < 0
            or dashboard_host != "127.0.0.1"
            or type(dashboard_port) is not int
            or not 1 <= dashboard_port <= 65535
        ):
            raise DashboardRelayError("invalid backend relay configuration")
        try:
            _private_directory(
                socket_path.parent, os.geteuid(), mode=0o2710,
                gid=frontend_gid,
            )
        except FenceWitnessError as exc:
            raise DashboardRelayError("unsafe relay socket directory") from exc
        if socket_path.exists() or socket_path.is_symlink():
            raise DashboardRelayError("refusing to replace a socket path")
        self._path = socket_path
        self._inode: tuple[int, int] | None = None
        self.frontend_uid = frontend_uid
        self.dashboard_port = dashboard_port
        super().__init__(str(socket_path), _BackendHandler)
        try:
            os.chmod(socket_path, 0o660)
            info = socket_path.lstat()
            self._inode = (info.st_dev, info.st_ino)
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_gid != frontend_gid
                or stat.S_IMODE(info.st_mode) != 0o660
            ):
                raise DashboardRelayError("relay socket ownership is unsafe")
        except BaseException:
            self.server_close()
            raise

    def server_close(self) -> None:
        super().server_close()
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and (
            info.st_dev, info.st_ino
        ) == self._inode:
            self._path.unlink()


class _FrontendHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        service: FrontendDashboardRelay = self.server  # type: ignore[assignment]
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as peer:
            try:
                peer.settimeout(5.0)
                peer.connect(str(service.socket_path))
                if _peer_uid(peer) != service.expected_owner_uid:
                    return
                _relay_streams(self.request, peer, stop=service._stopping)
            except (OSError, TimeoutError, ValueError, DashboardRelayError):
                return


class FrontendDashboardRelay(
    _LimitedThreading, socketserver.ThreadingMixIn, socketserver.TCPServer,
):
    """Unprivileged HTTP-only frontend. Has no CDP endpoint or file access."""

    allow_reuse_address = True
    request_queue_size = _MAX_PEERS

    def __init__(
        self, *, socket_path: Path, expected_owner_uid: int,
        listen_host: str = "0.0.0.0", listen_port: int = 8875,
    ) -> None:
        if (
            not isinstance(socket_path, Path) or not socket_path.is_absolute()
            or type(expected_owner_uid) is not int or expected_owner_uid < 0
            or listen_host not in ("0.0.0.0", "127.0.0.1")
            or type(listen_port) is not int or not 0 <= listen_port <= 65535
        ):
            raise DashboardRelayError("invalid frontend relay configuration")
        if not hasattr(socket, "SO_PEERCRED"):
            raise DashboardRelayError("kernel UNIX peer credentials unavailable")
        self.socket_path = socket_path
        self.expected_owner_uid = expected_owner_uid
        super().__init__((listen_host, listen_port), _FrontendHandler)

    @property
    def server_port(self) -> int:
        return self.server_address[1]


def relay_frontend_main(argv: list[str] | None = None) -> int:
    if container_draft_denied():
        print("mark-api: unattested container draft cannot run Python entrypoints",
              file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        description="Mark read-only transport frontend; never accepts a CDP address",
    )
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--expected-owner-uid", type=int, required=True)
    parser.add_argument("--port", type=int, default=8875)
    args = parser.parse_args(argv)
    if (
        args.expected_owner_uid < 0
        or args.expected_owner_uid == os.geteuid()
        or not args.socket.is_absolute()
    ):
        print("mark dashboard frontend: independent owner UID required",
              file=__import__("sys").stderr)
        return 2
    if _chrome_cdp_reachable():
        print("mark dashboard frontend: browser debugging isolation unavailable",
              file=__import__("sys").stderr)
        return 2
    try:
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as check:
            check.settimeout(5.0)
            check.connect(str(args.socket))
            if _peer_uid(check) != args.expected_owner_uid:
                raise DashboardRelayError("untrusted relay socket owner")
        service = FrontendDashboardRelay(
            socket_path=args.socket,
            expected_owner_uid=args.expected_owner_uid,
            listen_port=args.port,
        )
    except (OSError, ValueError, DashboardRelayError):
        print("mark dashboard frontend: trusted dashboard relay unavailable",
              file=__import__("sys").stderr)
        return 2
    stopping = Event()
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stopping.set())
    try:
        service.start()
        while not stopping.wait(.2):
            if service._serving is None or not service._serving.is_alive():
                return 2
    except KeyboardInterrupt:
        pass
    finally:
        completed = service.stop()
        signal.signal(signal.SIGTERM, previous)
    return 0 if completed else 2


# Production candidate: no IP stack exists in the untrusted frontend.
# A separate, deliberately small host-loopback ingress owns public TCP. It
# has no executor or witness socket mount; its only peer is this frontend UDS.
# A compromised frontend UID cannot create a route to host CDP/owner ports.
class _NetlessFrontendHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        service: NetlessFrontendDashboardRelay = self.server  # type: ignore[assignment]
        try:
            if _peer_uid(self.request) != service.ingress_uid:
                return
            with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as backend:
                backend.settimeout(5.0)
                backend.connect(str(service.backend_path))
                if _peer_uid(backend) != service.owner_uid:
                    return
                _relay_streams(self.request, backend, stop=service._stopping)
        except (OSError, TimeoutError, ValueError, DashboardRelayError):
            return


class NetlessFrontendDashboardRelay(
    _LimitedThreading, socketserver.ThreadingMixIn, socketserver.UnixStreamServer,
):
    """UID 50042 cannot create network traffic; accepts only UID 50045 ingress."""

    request_queue_size = _MAX_PEERS
    allow_reuse_address = False

    def __init__(
        self, *, socket_path: Path, backend_path: Path,
        ingress_uid: int, ingress_gid: int, owner_uid: int,
    ) -> None:
        if (
            not isinstance(socket_path, Path) or not socket_path.is_absolute()
            or not isinstance(backend_path, Path) or not backend_path.is_absolute()
            or socket_path == backend_path
            or any(type(value) is not int or value < 1
                   for value in (ingress_uid, ingress_gid, owner_uid))
            or len({ingress_uid, owner_uid, os.geteuid()}) != 3
            or not hasattr(socket, "SO_PEERCRED")
        ):
            raise DashboardRelayError("invalid netless frontend peer identities")
        try:
            _private_directory(
                socket_path.parent, os.geteuid(), mode=0o2710, gid=ingress_gid,
            )
        except FenceWitnessError as exc:
            raise DashboardRelayError("unsafe ingress socket directory") from exc
        if socket_path.exists() or socket_path.is_symlink():
            raise DashboardRelayError("refusing to replace ingress socket")
        self.socket_path = socket_path
        self.backend_path = backend_path
        self.owner_uid = owner_uid
        self.ingress_uid = ingress_uid
        self._inode: tuple[int, int] | None = None
        super().__init__(str(socket_path), _NetlessFrontendHandler)
        try:
            os.chmod(socket_path, 0o660)
            info = socket_path.lstat()
            self._inode = (info.st_dev, info.st_ino)
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_gid != ingress_gid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o660
            ):
                raise DashboardRelayError("netless ingress socket permissions unsafe")
        except BaseException:
            self.server_close()
            raise

    def server_close(self) -> None:
        super().server_close()
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and (
            info.st_dev, info.st_ino,
        ) == self._inode:
            self.socket_path.unlink()


class _PublicIngressHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        service: TrustedDashboardPublicIngress = self.server  # type: ignore[assignment]
        try:
            # Check the actual listening inode and the kernel peer identity on
            # every connection; a stale/swapped pathname never grants trust.
            before = service._validate_socket()
            with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as peer:
                peer.settimeout(5.0)
                peer.connect(str(service.socket_path))
                if _peer_uid(peer) != service.frontend_uid:
                    return
                after = service._validate_socket()
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    return
                _relay_streams(self.request, peer, stop=service._stopping)
        except (OSError, TimeoutError, ValueError, DashboardRelayError):
            return


class TrustedDashboardPublicIngress(
    _LimitedThreading, socketserver.ThreadingMixIn, socketserver.TCPServer,
):
    """Host-loopback TCP ingress with no CDP/SQLite/owner/witness mounts."""

    # Allow a verified clean restart while old HTTP connections are in TIME_WAIT.
    # SO_REUSEADDR does not allow a second simultaneously listening TCP socket;
    # SO_REUSEPORT stays disabled, and peer/UID/socket validation is unchanged.
    allow_reuse_address = True
    request_queue_size = _MAX_PEERS

    def __init__(
        self, *, socket_path: Path, frontend_uid: int,
        listen_host: str = "127.0.0.1", listen_port: int = 8875,
    ) -> None:
        if (
            not isinstance(socket_path, Path) or not socket_path.is_absolute()
            or type(frontend_uid) is not int or frontend_uid < 1
            or frontend_uid == os.geteuid()
            or listen_host != "127.0.0.1"
            or type(listen_port) is not int
            or not 0 <= listen_port <= 65535
            or not hasattr(socket, "SO_PEERCRED")
        ):
            raise DashboardRelayError("invalid public ingress identity/loopback bind")
        self.socket_path = socket_path
        self.frontend_uid = frontend_uid
        self._validate_socket()
        super().__init__((listen_host, listen_port), _PublicIngressHandler)

    def _validate_socket(self) -> os.stat_result:
        try:
            parent = self.socket_path.parent.lstat()
            info = self.socket_path.lstat()
        except OSError as exc:
            raise DashboardRelayError("frontend ingress socket unavailable") from exc
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != self.frontend_uid
            or parent.st_gid != os.getegid()
            or stat.S_IMODE(parent.st_mode) != 0o2710
            or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != self.frontend_uid
            or info.st_gid != os.getegid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o660
        ):
            raise DashboardRelayError("untrusted frontend ingress socket identity")
        return info


def relay_netless_frontend_main(argv: list[str] | None = None) -> int:
    if container_draft_denied():
        print("mark-api: unattested container draft cannot run Python entrypoints",
              file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Netless UID-bound dashboard frontend")
    parser.add_argument("--backend-socket", required=True, type=Path)
    parser.add_argument("--expected-owner-uid", required=True, type=int)
    parser.add_argument("--ingress-socket", required=True, type=Path)
    parser.add_argument("--ingress-uid", required=True, type=int)
    parser.add_argument("--ingress-gid", required=True, type=int)
    args = parser.parse_args(argv)
    # An existing stale socket blocks even before contacting the backend.
    try:
        args.ingress_socket.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return 2
    else:
        return 2
    try:
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as check:
            check.settimeout(5.0)
            check.connect(str(args.backend_socket))
            if _peer_uid(check) != args.expected_owner_uid:
                raise DashboardRelayError("untrusted owner socket")
        service = NetlessFrontendDashboardRelay(
            socket_path=args.ingress_socket, backend_path=args.backend_socket,
            ingress_uid=args.ingress_uid, ingress_gid=args.ingress_gid,
            owner_uid=args.expected_owner_uid,
        )
    except (OSError, ValueError, DashboardRelayError):
        print("mark netless frontend: trusted Unix sockets unavailable",
              file=sys.stderr)
        return 2
    stopping = Event()
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda _sig, _frame: stopping.set())
    try:
        service.start()
        while not stopping.wait(.2):
            if service._serving is None or not service._serving.is_alive():
                return 2
    except KeyboardInterrupt:
        pass
    finally:
        completed = service.stop()
        signal.signal(signal.SIGTERM, previous)
    return 0 if completed else 2


def relay_public_ingress_main(argv: list[str] | None = None) -> int:
    if container_draft_denied():
        print("mark-api: unattested container draft cannot run Python entrypoints",
              file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Bounded local Mark dashboard ingress")
    parser.add_argument("--frontend-socket", required=True, type=Path)
    parser.add_argument("--frontend-uid", required=True, type=int)
    parser.add_argument("--port", type=int, default=8875)
    args = parser.parse_args(argv)
    if args.port != 8875:
        print("mark public ingress: fixed localhost port 8875 required",
              file=sys.stderr)
        return 2
    try:
        service = TrustedDashboardPublicIngress(
            socket_path=args.frontend_socket, frontend_uid=args.frontend_uid,
            listen_port=args.port,
        )
    except DashboardRelayError as exc:
        # Fixed phase codes only: no paths, owner details or kernel errors.
        phase = {
            "frontend ingress socket unavailable":
                "mark public ingress: frontend socket stat unavailable",
            "untrusted frontend ingress socket identity":
                "mark public ingress: frontend socket metadata invalid",
        }.get(str(exc), "mark public ingress: frontend identity invalid")
        print(phase, file=sys.stderr)
        return 2
    except OSError as exc:
        # Kernel errno is a bounded category, never a logged exception string.
        if exc.errno in (errno.EACCES, errno.EPERM):
            phase = "mark public ingress: localhost bind permission denied"
        elif exc.errno == errno.EADDRINUSE:
            phase = "mark public ingress: localhost port occupied"
        elif exc.errno == errno.EADDRNOTAVAIL:
            phase = "mark public ingress: localhost address unavailable"
        else:
            phase = "mark public ingress: localhost listener unavailable"
        print(phase, file=sys.stderr)
        return 2
    except ValueError:
        print("mark public ingress: localhost listener unavailable",
              file=sys.stderr)
        return 2
    phase = "mark public ingress: frontend Unix connect unavailable"
    try:
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as check:
            check.settimeout(5.0)
            check.connect(str(args.frontend_socket))
            phase = "mark public ingress: frontend peer identity invalid"
            if _peer_uid(check) != args.frontend_uid:
                raise DashboardRelayError("wrong frontend peer UID")
    except (OSError, ValueError, DashboardRelayError):
        service.server_close()
        print(phase, file=sys.stderr)
        return 2
    stopping = Event()
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda _sig, _frame: stopping.set())
    try:
        service.start()
        while not stopping.wait(.2):
            if service._serving is None or not service._serving.is_alive():
                return 2
    except KeyboardInterrupt:
        pass
    finally:
        completed = service.stop()
        signal.signal(signal.SIGTERM, previous)
    return 0 if completed else 2


if __name__ == "__main__":
    if sys.argv[1:2] == ["netless-frontend"]:
        raise SystemExit(relay_netless_frontend_main(sys.argv[2:]))
    if sys.argv[1:2] == ["public-ingress"]:
        raise SystemExit(relay_public_ingress_main(sys.argv[2:]))
    raise SystemExit(relay_frontend_main())