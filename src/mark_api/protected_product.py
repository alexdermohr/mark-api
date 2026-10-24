"""One native protected customer start: preprovisioned A6 roles, not a new Write engine.

The root-only supervisor coordinates the installed Witness, CDP Actor, SQLite
Owner, full Dashboard and existing AF_UNIX ingress. Frontend/Owner share a
*separate network namespace* with only loopback, away from host CDP. The
host-loopback ingress gets irreversible Landlock TCP CONNECT denial.

--init-new provisions only a NEVER-before-used store under disjoint UIDs.
Existing customer database migration/restore is a separate host-cutover
operation: it is never silently performed by this entrypoint.
"""
from __future__ import annotations

import argparse
import ctypes
from contextlib import closing
import fcntl
import importlib.util
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import platform
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
from threading import Thread
from time import monotonic, sleep
from urllib.parse import quote

from .container_draft import container_draft_denied

FRONTEND, WITNESS, ACTOR, INGRESS, OWNER = 50042, 50043, 50044, 50045, 50046
GENERATOR = 50047
PRINCIPAL = "kleinanzeiger-protected-local"
PORT = 8875
DIRS = {
    "owner-data": (OWNER, OWNER, 0o700),
    "witness-data": (WITNESS, WITNESS, 0o700),
    "w-owner": (WITNESS, OWNER, 0o2710),
    "w-actor": (WITNESS, ACTOR, 0o2710),
    "actor-rpc": (ACTOR, OWNER, 0o2710),
    "dashboard-rpc": (OWNER, FRONTEND, 0o2710),
    "ingress-rpc": (FRONTEND, INGRESS, 0o2710),
    "actor-media": (ACTOR, ACTOR, 0o700),
}


class ProtectedProductError(RuntimeError):
    """Only sanitized startup/drain failure is exposed to customers."""


class _LandlockRuleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64)]


class _LandlockPort(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("port", ctypes.c_uint64)]


def _root_admission() -> None:
    if (sys.platform != "linux" or os.geteuid() != 0
            or not callable(getattr(os, "unshare", None))
            or not callable(getattr(os, "setns", None))
            or platform.machine() not in ("x86_64", "aarch64")):
        raise ProtectedProductError("root Linux namespace operator required")
    # No editable checkout or user-writable Python distribution under root.
    import mark_api
    source = Path(mark_api.__file__).resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    if "site-packages" not in source.parts:
        raise ProtectedProductError("installed immutable wheel required")
    for file in (source, python):
        info = file.stat()
        if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise ProtectedProductError("untrusted Python or installed package")


def _root_path(path: Path, *, fresh: bool) -> None:
    if (not isinstance(path, Path) or not path.is_absolute()
            or path != Path(os.path.normpath(path))):
        raise ProtectedProductError("canonical absolute state root required")
    current = Path("/")
    for segment in path.parts[1:]:
        current /= segment
        if not current.exists() and not current.is_symlink():
            if current != path or not fresh:
                raise ProtectedProductError("state parent missing")
            break
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise ProtectedProductError("untrusted state ancestor")
    if fresh:
        if path.exists() or path.is_symlink():
            raise ProtectedProductError("fresh install cannot adopt previous state")
    else:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o755):
            raise ProtectedProductError("protected state root differs")


def _paths(root: Path) -> dict[str, Path]:
    return {
        "db": root / "owner-data" / "snapshot.sqlite",
        "wdb": root / "witness-data" / "witness.sqlite",
        "wo": root / "w-owner" / "w.sock",
        "wa": root / "w-actor" / "w.sock",
        "actor": root / "actor-rpc" / "a.sock",
        "dashboard": root / "dashboard-rpc" / "d.sock",
        "ingress": root / "ingress-rpc" / "frontend.sock",
        "media": root / "actor-media" / "handles",
        "generation": root / "generation-rpc" / "g.sock",
        "lock": root / ".protected.lock",
    }


def _attest_existing(root: Path) -> None:
    for name, expected in DIRS.items():
        info = (root / name).lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
                != expected):
            raise ProtectedProductError("protected directory owner or mode differs")
    paths = _paths(root)
    for key, uid in (("db", OWNER), ("wdb", WITNESS)):
        info = paths[key].lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_gid != uid or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1):
            raise ProtectedProductError("preprovisioned journal identity invalid")
    for key in ("wo", "wa", "actor", "dashboard", "ingress"):
        if paths[key].exists() or paths[key].is_symlink():
            raise ProtectedProductError("stale socket requires independent reconciliation")


def _generation_admission() -> str:
    # Never create or mutate a customer database when opted-in provider
    # credentials/dependencies are missing. No paid provider contact here.
    key = os.environ.get("OPENAI_API_KEY")
    if (type(key) is not str or not 16 <= len(key) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in key)):
        raise ProtectedProductError("generation credential unavailable")
    try:
        if importlib.util.find_spec("openai") is None:
            raise ImportError()
        from openai import OpenAI
        if not callable(OpenAI):
            raise ImportError()
    except Exception:
        raise ProtectedProductError("generation SDK unavailable") from None
    return key


def _generation_directory(root: Path) -> None:
    # Optional opt-in directory is separate from the five-role baseline; a
    # previous installation can enable AI without recutting SQLite/Witness.
    path = root / "generation-rpc"
    if not path.exists() and not path.is_symlink():
        os.mkdir(path, 0o700)
        os.chown(path, GENERATOR, FRONTEND)
        os.chmod(path, 0o2710)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode)
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
               != (GENERATOR, FRONTEND, 0o2710)
            or (path / "g.sock").exists() or (path / "g.sock").is_symlink()):
        raise ProtectedProductError("generation worker directory invalid")


def _drop(uid: int, network_fd: int | None = None) -> None:
    if network_fd is not None:
        os.setns(network_fd, os.CLONE_NEWNET)
        os.close(network_fd)
    os.setgroups([])
    os.setresgid(uid, uid, uid)
    os.setresuid(uid, uid, uid)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise ProtectedProductError("no new privileges not enforced")


def _ingress_landlock() -> None:
    """Same ABI4 network rule as the existing root-owned ingress script."""
    if os.geteuid() != INGRESS:
        raise ProtectedProductError("wrong ingress UID")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def call(number: int, *args: object) -> int:
        result = libc.syscall(number, *args)
        if result < 0:
            raise ProtectedProductError("Landlock network policy unavailable")
        return int(result)

    if call(444, ctypes.c_void_p(), ctypes.c_size_t(), ctypes.c_uint(1)) < 4:
        raise ProtectedProductError("Landlock ABI4 required")
    # BIND/CONNECT are handled: one allowed BIND(8875), zero allowed CONNECT.
    access = _LandlockRuleset(0, 3)
    fd = call(444, ctypes.byref(access), ctypes.sizeof(access), ctypes.c_uint(0))
    try:
        port = _LandlockPort(1, PORT)
        call(445, ctypes.c_int(fd), ctypes.c_int(2),
             ctypes.byref(port), ctypes.c_uint(0))
        call(446, ctypes.c_int(fd), ctypes.c_uint(0))
    finally:
        os.close(fd)


def _generator_egress_landlock() -> None:
    """Allow AI worker HTTPS port 443 only; deny access to host CDP TCP.

    Linux Landlock network ABI4 governs all TCP connects from this process,
    including future provider libraries. This is applied in the pre-exec UID
    drop, before importing any optional provider code or reading the key.
    """
    if os.geteuid() != GENERATOR:
        raise ProtectedProductError("wrong generator UID")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def call(number: int, *args: object) -> int:
        value = libc.syscall(number, *args)
        if value < 0:
            raise ProtectedProductError("generator egress Landlock unavailable")
        return int(value)

    if call(444, ctypes.c_void_p(), ctypes.c_size_t(), ctypes.c_uint(1)) < 4:
        raise ProtectedProductError("generator Landlock ABI4 required")
    # Handle both TCP BIND and CONNECT: no TCP listener may be opened,
    # and only ordinary HTTPS 443 is allowed outbound. DNS UDP cannot
    # bypass TCP Landlock or gain an authenticated CDP browser route.
    ruleset = _LandlockRuleset(0, 3)
    fd = call(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), ctypes.c_uint(0))
    try:
        https = _LandlockPort(2, 443)
        call(445, ctypes.c_int(fd), ctypes.c_int(2),
             ctypes.byref(https), ctypes.c_uint(0))
        call(446, ctypes.c_int(fd), ctypes.c_uint(0))
    finally:
        os.close(fd)


def _private_network() -> int:
    host_fd = os.open("/proc/self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
    guest_fd = None
    try:
        os.unshare(os.CLONE_NEWNET)
        guest_fd = os.open("/proc/self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
                request = struct.pack("16sH14s", b"lo", 0, b"")
                flags = struct.unpack(
                    "16sH14s", fcntl.ioctl(peer.fileno(), 0x8913, request)
                )[1]
                fcntl.ioctl(peer.fileno(), 0x8914,
                            struct.pack("16sH14s", b"lo", flags | 1, b""))
        finally:
            os.setns(host_fd, os.CLONE_NEWNET)
        return guest_fd
    except BaseException:
        if guest_fd is not None:
            os.close(guest_fd)
        raise
    finally:
        os.close(host_fd)


def _uids_free(*, include_generator: bool = False) -> None:
    # Necessary check before a fresh start, not a substitute for independently
    # witnessed host-/PID-namespace history and writable FD/MAP_SHARED cutover.
    target = {FRONTEND, WITNESS, ACTOR, INGRESS, OWNER}
    if include_generator:
        target.add(GENERATOR)
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            line = next(row for row in (proc / "status").read_text().splitlines()
                        if row.startswith("Uid:"))
            owners = {int(x) for x in line.split()[1:]}
        except FileNotFoundError:
            continue
        except (OSError, ValueError, StopIteration):
            raise ProtectedProductError("process identity census incomplete") from None
        if target & owners:
            raise ProtectedProductError("service UID already holds a process")


def _initialize(root: Path) -> None:
    _root_path(root, fresh=True)
    os.mkdir(root, 0o755)
    os.chmod(root, 0o755)
    for name, (uid, gid, mode) in DIRS.items():
        subdir = root / name
        os.mkdir(subdir, 0o700)
        os.chown(subdir, uid, gid)
        os.chmod(subdir, mode)
    paths = _paths(root)
    # A one-shot fork under the exact *future owner UID* creates SQLite.
    # The privileged parent never opens database bytes or initializes a
    # missing ledger on an ordinary restart.
    for role, uid in (("owner", OWNER), ("witness", WITNESS)):
        pid = os.fork()
        if pid == 0:
            try:
                _drop(uid)
                if role == "owner":
                    from .storage import SnapshotStore
                    SnapshotStore(paths["db"], create_if_missing=True)
                else:
                    from .completion_witness import CompletionWitnessLedger
                    with CompletionWitnessLedger(
                        paths["wdb"], principal=PRINCIPAL, initialize=True,
                    ):
                        pass
                os._exit(0)
            except BaseException:
                os._exit(2)
        child, status = os.waitpid(pid, 0)
        if child != pid or not os.WIFEXITED(status) or os.WEXITSTATUS(status):
            # Never delete a partial generation or treat it as fresh again.
            raise ProtectedProductError("fresh storage provisioning failed")
    _attest_existing(root)


def _lock(root: Path) -> int:
    fd = os.open(root / ".protected.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1):
            raise ProtectedProductError("supervisor lock identity invalid")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _spawn(module: str, uid: int, *, net: int | None,
           config: dict[str, object] | None = None,
           args: tuple[str, ...] = (), ingress: bool = False) -> subprocess.Popen:
    argv = [sys.executable, "-I", "-m", module]
    rd = None
    if config is not None:
        payload = json.dumps(config, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
        if len(payload) > 8192:
            raise ProtectedProductError("private role frame oversized")
        rd, wr = os.pipe2(os.O_CLOEXEC)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(wr, view):]
        finally:
            os.close(wr)
        argv.extend(("--config-fd", str(rd)))
    else:
        argv.extend(args)
    try:
        def child_entry() -> None:
            _drop(uid, network_fd=net)
            if ingress:
                _ingress_landlock()
            elif module == "mark_api.protected_generation":
                _generator_egress_landlock()
        return subprocess.Popen(
            argv, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if ingress else subprocess.DEVNULL,
            close_fds=True, bufsize=0,
            pass_fds=tuple(x for x in (rd, net) if x is not None),
            preexec_fn=child_entry,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
                 "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"},
        )
    finally:
        if rd is not None:
            os.close(rd)


def _ready(child: subprocess.Popen, uid: int) -> dict[str, object]:
    if child.stdout is None:
        raise ProtectedProductError("missing role readiness channel")
    buf = bytearray()
    deadline = monotonic() + 20
    while monotonic() < deadline and len(buf) < 1024:
        if child.poll() is not None:
            raise ProtectedProductError("protected role exited before ready")
        readable, _, _ = select.select([child.stdout], [], [],
                                       min(.2, deadline - monotonic()))
        if readable:
            chunk = os.read(child.stdout.fileno(), 1024 - len(buf))
            if not chunk:
                raise ProtectedProductError("role readiness EOF")
            buf.extend(chunk)
            if b"\n" in buf:
                break
    try:
        if buf.count(b"\n") != 1 or not buf.endswith(b"\n"):
            raise ValueError()
        reply = json.loads(buf.decode("ascii"))
        if (type(reply) is not dict
                or set(reply) not in ({"ready", "uid"}, {"ready", "uid", "port"})
                or reply["ready"] is not True or
                type(reply["uid"]) is not int or reply["uid"] != uid):
            raise ValueError()
        if "port" in reply and (type(reply["port"]) is not int
                                or not 1 <= reply["port"] <= 65535):
            raise ValueError()
        return reply
    except (ValueError, UnicodeError):
        raise ProtectedProductError("unverified role readiness") from None


def _ready_ingress(proc: subprocess.Popen) -> None:
    deadline = monotonic() + 20
    while monotonic() < deadline:
        if proc.poll() is not None:
            # The existing relay emits one of a tiny set of fixed, sanitized
            # startup denials. Never echo arbitrary child stderr or a URL.
            pipe = getattr(proc, "stderr", None)
            try:
                raw = pipe.read(512).strip() if pipe is not None else b""
            except Exception:
                raw = b""
            ingress_phases = {
                b"mark public ingress: verified frontend unavailable":
                    "ingress frontend socket identity unavailable",
                b"mark public ingress: frontend socket stat unavailable":
                    "ingress frontend socket stat unavailable",
                b"mark public ingress: frontend socket metadata invalid":
                    "ingress frontend socket metadata invalid",
                b"mark public ingress: frontend identity invalid":
                    "ingress frontend identity invalid",
                b"mark public ingress: localhost listener unavailable":
                    "ingress localhost listener unavailable",
                b"mark public ingress: localhost bind permission denied":
                    "ingress localhost bind permission denied",
                b"mark public ingress: localhost port occupied":
                    "ingress localhost port occupied",
                b"mark public ingress: localhost address unavailable":
                    "ingress localhost address unavailable",
                b"mark public ingress: frontend Unix connect unavailable":
                    "ingress frontend Unix connect unavailable",
                b"mark public ingress: frontend peer identity invalid":
                    "ingress frontend peer identity invalid",
            }
            if raw in ingress_phases:
                raise ProtectedProductError(ingress_phases[raw])
            if raw == b"mark public ingress: fixed localhost port 8875 required":
                raise ProtectedProductError("ingress fixed port refused")
            if raw == b"mark-api: unattested container draft cannot run Python entrypoints":
                raise ProtectedProductError("ingress image draft denied")
            raise ProtectedProductError("trusted HTTP ingress unavailable")
        try:
            with closing(HTTPConnection("127.0.0.1", PORT, timeout=0.5)) as http:
                http.request("GET", "/readyz")
                response = http.getresponse()
                response.read(2048)
                if response.status == 200:
                    return
        except OSError:
            pass
        sleep(0.1)
    raise ProtectedProductError("customer dashboard ingress did not become ready")


def _stop(children: list[tuple[str, subprocess.Popen]]) -> bool:
    success = True
    for name, child in reversed(children):
        try:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=150 if name == "generation" else 90 if name == "owner" else 35)
                if child.returncode != 0:
                    success = False
            except subprocess.TimeoutExpired:
                success = False
                child.kill()
                child.wait(timeout=5)
        except Exception:
            # Reaping a concurrently crashed child must NOT overwrite the
            # original witness/actor startup error or mint a success result.
            success = False
            try:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
            except Exception:
                pass
        finally:
            if child.stdout is not None:
                try:
                    child.stdout.close()
                except Exception:
                    success = False
    return success


def run(*, root: Path, init_new: bool = False, cdp_port: int = 9222,
        sync_interval: int = 300, enable_generation: bool = False) -> int:
    # Emit only static phase names on unexpected host/runtime errors. The
    # customer-facing CLI never logs a UID token, secret, path or traceback.
    def admitted(label, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except ProtectedProductError:
            raise
        except Exception:
            raise ProtectedProductError(label) from None

    admitted("root runtime admission failed", _root_admission)
    if container_draft_denied():
        raise ProtectedProductError("draft cannot start protected product")
    if (type(cdp_port) is not int or not 1 <= cdp_port <= 65535
            or type(sync_interval) is not int or
            sync_interval != 0 and not 60 <= sync_interval <= 86400
            or type(enable_generation) is not bool
            or (enable_generation and cdp_port == 443)):
        raise ProtectedProductError("invalid CDP port/sync cadence")
    generation_key = admitted("generation admission failed", _generation_admission) if enable_generation else None
    admitted("state root inspection failed", _root_path, root, fresh=init_new)
    admitted("service UID census failed", _uids_free, include_generator=enable_generation)
    if init_new:
        admitted("fresh state provisioning failed", _initialize, root)
    else:
        admitted("protected state attestation failed", _attest_existing, root)
    paths = _paths(root)
    lock = admitted("supervisor lock admission failed", _lock, root)
    net = None
    children: list[tuple[str, subprocess.Popen]] = []
    stopped = [False]

    def request_stop(_sig, _frame):
        stopped[0] = True

    previous_term = signal.signal(signal.SIGTERM, request_stop)
    previous_int = signal.signal(signal.SIGINT, request_stop)
    outcome = 2
    try:
        try:
            net = _private_network()
        except Exception:
            raise ProtectedProductError("private network namespace unavailable") from None
        api = secrets.token_urlsafe(48)
        ui = secrets.token_urlsafe(48)
        gen_ui = secrets.token_urlsafe(48) if enable_generation else None
        if enable_generation:
            admitted("generation directory admission failed", _generation_directory, root)

        def start(name: str, module: str, uid: int,
                  payload: dict[str, object], *, private: bool):
            try:
                child = _spawn(module, uid, net=net if private else None,
                               config=payload)
            except Exception:
                raise ProtectedProductError(name + " process admission failed") from None
            children.append((name, child))
            try:
                return _ready(child, uid)
            except Exception:
                raise ProtectedProductError(name + " readiness unavailable") from None

        start("witness", "mark_api.protected_witness", WITNESS, {
            "database": str(paths["wdb"]),
            "owner_socket": str(paths["wo"]),
            "actor_socket": str(paths["wa"]),
            "owner_uid": OWNER, "owner_gid": OWNER,
            "actor_uid": ACTOR, "actor_gid": ACTOR,
            "runtime_uid": FRONTEND, "principal": PRINCIPAL,
        }, private=True)
        start("actor", "mark_api.protected_actor", ACTOR, {
            "socket": str(paths["actor"]),
            "witness_socket": str(paths["wa"]),
            "owner_uid": OWNER, "owner_gid": OWNER,
            "witness_uid": WITNESS, "runtime_uid": FRONTEND,
            "principal": PRINCIPAL, "cdp_port": cdp_port,
            "timeout_seconds": 5.0, "media_enabled": True,
            "media_directory": str(paths["media"]),
        }, private=False)
        owner = start("owner", "mark_api.protected_owner", OWNER, {
            "database": str(paths["db"]),
            "dashboard_socket": str(paths["dashboard"]),
            "witness_socket": str(paths["wo"]),
            "actor_socket": str(paths["actor"]),
            "witness_uid": WITNESS, "actor_uid": ACTOR,
            "runtime_uid": FRONTEND, "runtime_gid": FRONTEND,
            "principal": PRINCIPAL, "bearer_token": api,
            "media_enabled": True, "write_port": 0,
            "sync_interval_seconds": sync_interval,
        }, private=True)
        if enable_generation:
            start("generation", "mark_api.protected_generation", GENERATOR, {
                "socket": str(paths["generation"]),
                "frontend_uid": FRONTEND,
                "generation_key": generation_key,
            }, private=False)
        frontend_payload = {
            "owner_socket": str(paths["dashboard"]),
            "owner_uid": OWNER, "principal": PRINCIPAL,
            "owner_port": owner["port"], "api_token": api,
            "ui_token": ui, "dashboard_port": 0,
            "ingress_socket": str(paths["ingress"]),
            "ingress_uid": INGRESS, "ingress_gid": INGRESS,
        }
        if enable_generation:
            frontend_payload.update({
                "generation_token": gen_ui,
                "generation_socket": str(paths["generation"]),
                "generator_uid": GENERATOR,
            })
        frontend = start("frontend", "mark_api.protected_frontend", FRONTEND,
                         frontend_payload, private=True)
        if frontend["port"] == owner["port"]:
            raise ProtectedProductError("backend and dashboard shared a port")
        try:
            ingress = _spawn(
                "mark_api.dashboard_relay", INGRESS, net=None,
                args=("public-ingress", "--frontend-socket", str(paths["ingress"]),
                      "--frontend-uid", str(FRONTEND), "--port", str(PORT)),
                ingress=True,
            )
        except Exception:
            raise ProtectedProductError("Landlock ingress admission failed") from None
        children.append(("ingress", ingress))
        _ready_ingress(ingress)
        # Drain discarded runtime errors without ever serializing HTTP
        # contents, cookies or provider secrets in root-owned startup logs.
        pipe = getattr(ingress, "stderr", None)
        if pipe is not None:
            def discard_ingress_stderr():
                try:
                    while pipe.read(4096):
                        pass
                except Exception:
                    pass
            Thread(target=discard_ingress_stderr, daemon=True).start()
        # The write token is only in a fragment, not the server HTTP logs.
        public_fragment = "write_token=" + quote(ui, safe="")
        if gen_ui is not None:
            public_fragment += "&generation_token=" + quote(gen_ui, safe="")
        print("Kleinanzeiger: http://127.0.0.1:" + str(PORT)
              + "/#" + public_fragment, flush=True)
        while not stopped[0]:
            if any(process.poll() is not None for _, process in children):
                raise ProtectedProductError("protected role terminated")
            sleep(.2)
        outcome = 0
    finally:
        if not _stop(children):
            outcome = 2
        if net is not None:
            os.close(net)
        os.close(lock)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kleinanzeiger one-command protected fresh-install/customer start",
    )
    parser.add_argument("--state-root", type=Path, default=Path("/var/lib/kleinanzeiger"))
    parser.add_argument("--init-new", action="store_true",
                        help="One-time NEW owner/Witness store; never import legacy state")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--sync-interval", type=int, default=300)
    parser.add_argument("--enable-generation", action="store_true",
                        help="Opt-in prompt generation with per-request provider cost consent")
    args = parser.parse_args(argv)
    old_umask = os.umask(0o077)
    try:
        try:
            return run(root=args.state_root, init_new=args.init_new,
                       cdp_port=args.cdp_port, sync_interval=args.sync_interval,
                       enable_generation=args.enable_generation)
        except ProtectedProductError as error:
            # All these error messages are authored static admission phases.
            print("Kleinanzeiger: protected customer startup unavailable ("
                  + str(error) + ")", file=sys.stderr)
            return 2
        except Exception as error:
            # Builtin exception *types* only. No exception message, traceback,
            # source path, filesystem identity, SQL value or credential.
            kind = type(error).__name__
            if type(error).__module__ != "builtins" or kind not in {
                "OSError", "FileNotFoundError", "PermissionError", "RuntimeError",
                "TypeError", "ValueError", "AttributeError", "KeyError",
                "IndexError", "ImportError", "TimeoutError",
                "ConnectionError", "BrokenPipeError", "MemoryError",
            }:
                kind = "unknown"
            print("Kleinanzeiger: protected customer startup unavailable (internal "
                  + kind + ")", file=sys.stderr)
            return 2
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())