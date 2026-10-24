"""Optional protected text/image draft provider behind a peer-authenticated UNIX socket.

The customer dashboard/SQLite owner stay in their isolated netns. Only this
separate, non-owner UID has provider egress and the OpenAI key. No provider is
called before an explicitly acknowledged prompt request; no retries or
platform mutations exist on this channel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import stat
import struct
import sys
from threading import Lock, Thread

from .generation import (
    AdvertisementDraft, AdvertisementGenerator, GenerationError,
    _validate_png_payload, _validated_prompt,
)
from .strict_json import strict_json_object

_MAX_FRAME = 8192
_MAX_REPLY = 32768
_MAX_IMAGE = 18_000_000
_KEY_LIMIT = 4096
_LIMIT = 10


class ProtectedGenerationError(RuntimeError):
    """Sanitized provider boundary failure."""


def _canon(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise ValueError("canonical generation socket required")
    return path


def _peer_uid(peer: socket.socket) -> int:
    raw = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    if len(raw) != 12:
        raise ProtectedGenerationError("unverified generation peer")
    return struct.unpack("3i", raw)[1]


def _wire(data: dict[str, object]) -> bytes:
    return json.dumps(data, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("ascii") + b"\n"


class _GenerationHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        # socketserver otherwise prints worker paths and tracebacks when a
        # verified peer disconnects before reading an error or generated PNG.
        try:
            self._handle_bounded()
        except Exception:
            return

    def _handle_bounded(self) -> None:
        service: ProtectedGenerationServer = self.server
        self.request.settimeout(140)
        if _peer_uid(self.request) != service.frontend_uid:
            return
        try:
            raw = self.rfile.readline(_MAX_FRAME + 1)
            if not raw.endswith(b"\n") or not 2 <= len(raw) <= _MAX_FRAME:
                raise ProtectedGenerationError("invalid generation frame")
            request = strict_json_object(raw[:-1])
            if (set(request) != {"version", "mode", "prompt", "acknowledge_provider_cost"}
                    or type(request["version"]) is not int or request["version"] != 1
                    or request["mode"] not in ("text", "image")
                    or request["acknowledge_provider_cost"] is not True):
                raise ProtectedGenerationError("invalid generation request")
            prompt = _validated_prompt(request["prompt"])
        except Exception:
            self.wfile.write(_wire({"version": 1, "ok": False, "error": "invalid_generation_request"}))
            return
        # A cost-bearing call spends exactly one slot before provider contact.
        if not service._cost_lock.acquire(blocking=False):
            self.wfile.write(_wire({"version": 1, "ok": False, "error": "generation_busy"}))
            return
        try:
            if service._used >= _LIMIT:
                self.wfile.write(_wire({"version": 1, "ok": False, "error": "generation_budget_exhausted"}))
                return
            service._used += 1
            try:
                generator = service.generator_factory()
                if request["mode"] == "text":
                    draft = generator.generate_text(prompt)
                    if not isinstance(draft, AdvertisementDraft):
                        raise ProtectedGenerationError("unexpected draft")
                    reply = _wire({"version": 1, "ok": True, "kind": "text",
                                   "title": draft.title, "description": draft.description})
                    if len(reply) > _MAX_REPLY:
                        raise ProtectedGenerationError("oversized draft")
                    self.wfile.write(reply)
                else:
                    png = generator.generate_image(prompt)
                    _validate_png_payload(png)
                    if len(png) > _MAX_IMAGE:
                        raise ProtectedGenerationError("oversized image")
                    self.wfile.write(_wire({
                        "version": 1, "ok": True, "kind": "image",
                        "length": len(png), "sha256": hashlib.sha256(png).hexdigest(),
                    }))
                    self.wfile.write(png)
                self.wfile.flush()
            except Exception:
                # Never expose provider key, prompt, exception text or trace.
                self.wfile.write(_wire({"version": 1, "ok": False, "error": "generation_failed"}))
        finally:
            service._cost_lock.release()


class ProtectedGenerationServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """One dedicated worker UID, private socket, UID-authenticated requests."""

    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False

    def __init__(self, *, socket_path: Path, frontend_uid: int,
                 generator_factory=AdvertisementGenerator) -> None:
        socket_path = _canon(socket_path)
        if type(frontend_uid) is not int or frontend_uid <= 0 or not callable(generator_factory):
            raise ValueError("invalid generation actor parameters")
        parent = socket_path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid() or parent.st_gid != frontend_uid
                or stat.S_IMODE(parent.st_mode) != 0o2710
                or socket_path.exists() or socket_path.is_symlink()):
            raise ValueError("generation socket ownership unavailable")
        self.frontend_uid = frontend_uid
        self.generator_factory = generator_factory
        self._used = 0
        self._cost_lock = Lock()
        self._path = socket_path
        super().__init__(str(socket_path), _GenerationHandler)
        os.chmod(socket_path, 0o660)
        info = socket_path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_gid != frontend_uid or stat.S_IMODE(info.st_mode) != 0o660):
            super().server_close()
            raise ValueError("generation socket identity invalid")
        self._identity = (info.st_dev, info.st_ino)

    def server_close(self) -> None:
        super().server_close()
        info = self._path.lstat()
        if ((info.st_dev, info.st_ino) != self._identity
                or not stat.S_ISSOCK(info.st_mode)):
            raise ProtectedGenerationError("generation socket replaced")
        self._path.unlink()


class ProtectedGenerationClient:
    """Netless frontend proxy: one bounded authenticated request, no retry."""

    def __init__(self, *, socket_path: Path, worker_uid: int) -> None:
        self.path = _canon(socket_path)
        if type(worker_uid) is not int or worker_uid <= 0:
            raise ValueError("invalid generation server UID")
        self.worker_uid = worker_uid

    def _call(self, mode: str, prompt: str) -> AdvertisementDraft | bytes:
        prompt = _validated_prompt(prompt)
        if mode not in ("text", "image"):
            raise GenerationError("invalid generator mode")
        frame = _wire({"version": 1, "mode": mode, "prompt": prompt,
                       "acknowledge_provider_cost": True})
        if len(frame) > _MAX_FRAME:
            raise GenerationError("invalid generation frame")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(140)
                sock.connect(str(self.path))
                if _peer_uid(sock) != self.worker_uid:
                    raise ProtectedGenerationError("untrusted generation provider")
                sock.sendall(frame)
                with sock.makefile("rb") as response:
                    raw = response.readline(_MAX_REPLY + 1)
                    if not raw.endswith(b"\n") or len(raw) > _MAX_REPLY:
                        raise ProtectedGenerationError("malformed generation reply")
                    row = strict_json_object(raw[:-1])
                    if row.get("version") != 1 or row.get("ok") is not True:
                        raise ProtectedGenerationError("generation unavailable")
                    if mode == "text":
                        if set(row) != {"version", "ok", "kind", "title", "description"} or row["kind"] != "text":
                            raise ProtectedGenerationError("invalid text draft")
                        if type(row["title"]) is not str or type(row["description"]) is not str:
                            raise ProtectedGenerationError("invalid draft type")
                        return AdvertisementDraft(title=row["title"], description=row["description"])
                    length = row.get("length")
                    digest = row.get("sha256")
                    if (set(row) != {"version", "ok", "kind", "length", "sha256"}
                            or row["kind"] != "image"
                            or type(length) is not int or not 45 <= length <= _MAX_IMAGE
                            or type(digest) is not str or len(digest) != 64):
                        raise ProtectedGenerationError("invalid image frame")
                    data = response.read(length)
                    if len(data) != length or hashlib.sha256(data).hexdigest() != digest:
                        raise ProtectedGenerationError("invalid image digest")
                    _validate_png_payload(data)
                    return data
        except Exception:
            raise GenerationError("protected generation unavailable") from None

    def generate_text(self, prompt: str) -> AdvertisementDraft:
        result = self._call("text", prompt)
        if not isinstance(result, AdvertisementDraft):
            raise GenerationError("invalid generator text")
        return result

    def generate_image(self, prompt: str) -> bytes:
        result = self._call("image", prompt)
        if not isinstance(result, bytes):
            raise GenerationError("invalid generator image")
        return result


def _private_config(fd: int) -> dict[str, object]:
    if type(fd) is not int or fd < 0:
        raise ValueError("private generator config required")
    info = os.fstat(fd)
    if not stat.S_ISFIFO(info.st_mode):
        raise ValueError("private generator pipe required")
    with os.fdopen(os.dup(fd), "rb") as source:
        data = source.read(_MAX_FRAME + 1)
    if not 3 <= len(data) <= _MAX_FRAME or not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise ValueError("invalid generator config frame")
    config = strict_json_object(data[:-1])
    if set(config) != {"socket", "frontend_uid", "generation_key"}:
        raise ValueError("invalid generator config")
    if (type(config["socket"]) is not str
            or type(config["frontend_uid"]) is not int or config["frontend_uid"] <= 0
            or type(config["generation_key"]) is not str
            or not 16 <= len(config["generation_key"]) <= _KEY_LIMIT
            or any(ord(c) < 33 or ord(c) > 126 for c in config["generation_key"])):
        raise ValueError("invalid generator config data")
    _canon(Path(config["socket"]))
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private Kleinanzeiger provider worker")
    parser.add_argument("--config-fd", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        config = _private_config(args.config_fd)
        if os.geteuid() in (0, config["frontend_uid"]):
            raise ValueError("generator UID must be separate and nonroot")
        from openai import OpenAI
        if not callable(OpenAI):
            raise ValueError("generation SDK missing")
        os.environ["OPENAI_API_KEY"] = config["generation_key"]
        server = ProtectedGenerationServer(
            socket_path=Path(config["socket"]),
            frontend_uid=config["frontend_uid"],
        )
    except Exception:
        print("Kleinanzeiger: generation worker unavailable (admission denied)", file=sys.stderr)
        return 2
    def stop(_signal, _frame):
        Thread(target=server.shutdown, daemon=True).start()
    old = signal.signal(signal.SIGTERM, stop)
    try:
        print(json.dumps({"ready": True, "uid": os.geteuid()}, separators=(",", ":")), flush=True)
        server.serve_forever(poll_interval=0.1)
    finally:
        signal.signal(signal.SIGTERM, old)
        try:
            server.server_close()
        except Exception:
            print("Kleinanzeiger: generation worker unavailable (shutdown uncertain)", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())