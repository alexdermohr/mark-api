"""Protected installed A6 dashboard. A trusted supervisor must provision the
distinct actor, owner and witness UIDs; this module grants no host cutover.
No direct SQLite, local completion bypass or platform login is permitted.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import sys
from threading import Thread

from .completion_dashboard import CompletionDashboardClient
from .container_draft import container_draft_denied
from .dashboard import DashboardGenerationAccess, DashboardWriteProxy, create_server
from .storage_owner import StorageOwnerError
from .strict_json import strict_json_object

_FIELDS = frozenset(("owner_socket", "owner_uid", "principal", "owner_port",
                     "api_token", "ui_token", "dashboard_port"))
_GENERATION_FIELDS = _FIELDS | {"generation_token"}
_GENERATOR_FIELDS = _GENERATION_FIELDS | {"generation_socket", "generator_uid"}
_INGRESS_FIELDS = frozenset(("ingress_socket", "ingress_uid", "ingress_gid"))
_INGRESS_PORT = 8875
_LIMIT = 8192


def create_protected_frontend_server(
    *, owner_socket: Path, owner_uid: int, principal: str,
    owner_port: int, api_token: str, ui_token: str, dashboard_port: int = 8765,
    generation_access: DashboardGenerationAccess | None = None,
    public_origin_port: int | None = None,
):
    """Use the real strict owner/Witness-backed Write proxy, never a test seam."""
    if not isinstance(owner_socket, Path) or not owner_socket.is_absolute():
        raise ValueError("absolute independent owner socket required")
    client = CompletionDashboardClient(
        owner_socket, expected_owner_uid=owner_uid, expected_principal=principal,
    )
    proxy = DashboardWriteProxy(
        host="127.0.0.1", port=owner_port, bearer_token=api_token,
        ui_token=ui_token, backend_principal=principal,
        owner_reconciliation_enabled=True,
    )
    options = {"generation_access": generation_access}
    if public_origin_port is not None:
        options["public_origin_port"] = public_origin_port
    return create_server(
        client, port=dashboard_port, write_proxy=proxy, **options,
    )


def _read_private_config(fd: int) -> dict[str, object]:
    """Get secrets only over one bounded inherited private pipe/file FD."""
    if type(fd) is not int or fd < 0:
        raise ValueError("private configuration descriptor required")
    info = os.fstat(fd)
    if stat.S_ISREG(info.st_mode):
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid not in (0, os.geteuid()):
            raise ValueError("private configuration descriptor required")
    elif not stat.S_ISFIFO(info.st_mode):
        raise ValueError("private configuration descriptor required")
    with os.fdopen(os.dup(fd), "rb") as source:
        raw = source.read(_LIMIT + 1)
    if (len(raw) < 3 or len(raw) > _LIMIT
            or not raw.endswith(b"\n") or raw.count(b"\n") != 1):
        raise ValueError("invalid protected frontend configuration")
    config = strict_json_object(raw[:-1])
    if set(config) not in (_FIELDS, _GENERATION_FIELDS, _FIELDS | _INGRESS_FIELDS,
                           _GENERATION_FIELDS | _INGRESS_FIELDS,
                           _GENERATOR_FIELDS, _GENERATOR_FIELDS | _INGRESS_FIELDS) or type(config["owner_socket"]) is not str:
        raise ValueError("invalid protected frontend configuration")
    if not config["owner_socket"].startswith("/"):
        raise ValueError("invalid protected frontend configuration")
    if any(type(config[field]) is not int
           for field in ("owner_uid", "owner_port", "dashboard_port")):
        raise ValueError("invalid protected frontend configuration")
    if _INGRESS_FIELDS <= set(config):
        from .completion_authority import _roles
        path = config["ingress_socket"]
        if (type(path) is not str or not path.startswith("/")
                or Path(path) != Path(os.path.normpath(path))
                or Path(path) == Path(config["owner_socket"])
                or any(type(config[k]) is not int or config[k] <= 0
                       for k in ("ingress_uid", "ingress_gid"))
                or config["ingress_uid"] != config["ingress_gid"]):
            raise ValueError("invalid private ingress profile")
        _roles(os.geteuid(), config["owner_uid"], config["ingress_uid"])
    if "generation_token" in config:
        token = config["generation_token"]
        if (type(token) is not str or not 16 <= len(token) <= 512
                or any(not 0x21 <= ord(char) <= 0x7E for char in token)):
            raise ValueError("invalid private generation token")
    if "generation_socket" in config:
        location = config["generation_socket"]
        uid = config["generator_uid"]
        if (type(location) is not str or not location.startswith("/")
                or Path(location) != Path(os.path.normpath(location))
                or Path(location) == Path(config["owner_socket"])
                or location == config.get("ingress_socket")
                or type(uid) is not int or uid <= 0
                or uid in (os.geteuid(), config["owner_uid"], config.get("ingress_uid"))):
            raise ValueError("invalid private generator identity")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kleinanzeiger protected frontend for a preprovisioned A6 owner")
    parser.add_argument("--config-fd", type=int, default=0,
                        help="Inherited private configuration FD (no secrets in argv)")
    args = parser.parse_args(argv)
    # Only bounded, nonsensitive startup phases may be printed. Socket paths,
    # configuration bytes, tokens and exception strings are never logged.
    if container_draft_denied():
        print("Kleinanzeiger: protected frontend unavailable (draft denied)",
              file=sys.stderr)
        return 2
    try:
        config = _read_private_config(args.config_fd)
    except (ValueError, TypeError, OSError):
        print("Kleinanzeiger: protected frontend unavailable (config rejected)",
              file=sys.stderr)
        return 2
    # Generation is opt-in, using the same per-session cost-consent gate as
    # the normal customer launcher. Never construct a paid provider at start.
    generation_access = None
    generation_token = config.get("generation_token")
    if generation_token is not None:
        if "generation_socket" in config:
            # Protected installs keep frontend netless and credential-free.
            # A separate peer-verified worker handles each explicitly
            # consented provider call, never a marketplace Write.
            from .protected_generation import ProtectedGenerationClient
            socket_path = Path(config["generation_socket"])
            worker_uid = config["generator_uid"]
            generation_access = DashboardGenerationAccess(
                ui_token=generation_token,
                generator_factory=lambda: ProtectedGenerationClient(
                    socket_path=socket_path, worker_uid=worker_uid,
                ),
            )
        else:
            # Legacy individually provisioned frontend composition.
            if not os.environ.get("OPENAI_API_KEY"):
                print("Kleinanzeiger: protected frontend unavailable (generation credential missing)",
                      file=sys.stderr)
                return 2
            try:
                from openai import OpenAI as sdk
                if not callable(sdk):
                    raise TypeError("generation SDK unavailable")
            except Exception:
                print("Kleinanzeiger: protected frontend unavailable (generation SDK missing)",
                      file=sys.stderr)
                return 2
            generation_access = DashboardGenerationAccess(ui_token=generation_token)
    try:
        server = create_protected_frontend_server(
            owner_socket=Path(config["owner_socket"]),
            owner_uid=config["owner_uid"], principal=config["principal"],
            owner_port=config["owner_port"], api_token=config["api_token"],
            ui_token=config["ui_token"], dashboard_port=config["dashboard_port"],
            generation_access=generation_access,
            **({"public_origin_port": _INGRESS_PORT}
               if "ingress_socket" in config else {}),
        )
    except (ValueError, TypeError, OSError, StorageOwnerError):
        print("Kleinanzeiger: protected frontend unavailable (owner unavailable)",
              file=sys.stderr)
        return 2

    # A dedicated host-loopback ingress may reach this full customer UI only
    # through the existing SO_PEERCRED-verified UNIX relay. The browser-facing
    # frontend process stays in a shared OWNER-only private network namespace:
    # no CDP, external provider or host-network route is minted here.
    relay = None
    try:
        if "ingress_socket" in config:
            from .dashboard_relay import UnixDashboardRelay, DashboardRelayError
            relay = UnixDashboardRelay(
                socket_path=Path(config["ingress_socket"]),
                frontend_uid=config["ingress_uid"],
                frontend_gid=config["ingress_gid"],
                dashboard_host="127.0.0.1", dashboard_port=server.server_port,
            )
            relay.start()
            if relay._serving is None or not relay._serving.is_alive():
                raise DashboardRelayError("frontend ingress unavailable")
    except Exception:
        if relay is not None:
            try:
                relay.stop(timeout_seconds=1.0)
            except Exception:
                pass
        server.server_close()
        print("Kleinanzeiger: protected frontend unavailable (ingress admission denied)",
              file=sys.stderr)
        return 2

    def stop(_sig, _frame):
        Thread(target=server.shutdown, daemon=True).start()

    old = signal.signal(signal.SIGTERM, stop)
    status = 0
    try:
        # Only the supervisor, not this service's stdout, publishes the
        # fragment-token customer URL. Readiness contains no private bearer.
        print(json.dumps({"ready": True, "uid": os.geteuid(), "port": server.server_port},
                         separators=(",", ":")), flush=True)
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    except Exception:
        status = 2
    finally:
        signal.signal(signal.SIGTERM, old)
        if relay is not None:
            try:
                if not relay.stop(timeout_seconds=25.0):
                    status = 2
            except Exception:
                status = 2
        try:
            server.server_close()
        except Exception:
            status = 2
    if status:
        print("Kleinanzeiger: protected frontend unavailable (shutdown uncertain)",
              file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())