"""Installed non-media A6 acceptance in a disposable Linux PID/net namespace.

The root supervisor owns only its temporary directory and child processes.
Actual product roles use different UIDs, no capabilities and no_new_privs.
No real platform, provider or Docker access. This is not old-inode host cutover
or complete backup/media release proof. Never run directly in a host namespace.
"""
from __future__ import annotations

import argparse
from base64 import b64decode, b64encode
from contextlib import closing
import ctypes
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import mark_api
from mark_api.application import MarkService
from mark_api.completion_authority import CompletionAuthorityServer, create_owner_write_api_server
from mark_api.completion_witness import AuthorityWitnessClient, CompletionWitnessLedger, CompletionWitnessServer
from mark_api.completion_dashboard import CompletionDashboardOwnerServer
from mark_api.domain import AdSnapshot, LifecycleState
from mark_api.private_web import PrivateWebCreateSnapshot, PrivateWebEditorState
from mark_api.private_web_media import (
    PrivateWebCreateMediaSnapshot, PrivateWebMediaFileSnapshot, PrivateWebMediaHandleStore,
    PrivateWebMediaPersistenceSnapshot,
)
from mark_api.private_web_runtime import PrivateWebMediaCreateRuntime, PrivateWebMediaCreateService
from mark_api.fence_witness import FenceWitnessClient, _WitnessUnixHandler
from mark_api.results import ReadResult, ReadStatus
from mark_api.storage import SnapshotStore
from mark_api.write_api import WriteApiAccess, WriteCapability

UIDS = {"owner": 50111, "actor": 50112, "witness": 50113, "platform": 50114, "runtime": 50115}
PRINCIPAL = "synthetic-protected-customer"
# A deterministic 1x1 PNG, never sent outside this disposable net namespace.
MEDIA_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Zr4jV8AAAAASUVORK5CYII="
)


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _emit(value):
    print(_json(value), flush=True)


def _drop(uid):
    assert os.geteuid() == 0 and uid in UIDS.values()
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(38, 1, 0, 0, 0) == 0  # PR_SET_NO_NEW_PRIVS
    libc.prctl(47, 4, 0, 0, 0)  # PR_CAP_AMBIENT_CLEAR_ALL
    for capability in range(64):
        result = libc.prctl(24, capability, 0, 0, 0)  # PR_CAPBSET_DROP
        assert result == 0 or ctypes.get_errno() == 22
    os.setgroups([])
    os.setresgid(uid, uid, uid)
    os.setresuid(uid, uid, uid)
    assert libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE after changing UID
    os.umask(0o077)
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    assert all(int(status[key].strip(), 16) == 0 for key in ("CapEff", "CapPrm", "CapInh", "CapBnd", "CapAmb"))
    assert status["NoNewPrivs"].strip() == "1" and os.getgroups() == []
    assert os.getresuid() == (uid, uid, uid) and os.getresgid() == (uid, uid, uid)


def _http(port, method, path, *, token, payload=None, key=None, ui_token=None, raw=False, media=None):
    headers = ({"Authorization": "Bearer " + token} if ui_token is None else {
        "Origin": "http://127.0.0.1:" + str(port), "X-Mark-Dashboard-Write": "1",
        "X-Mark-Dashboard-Token": ui_token,
    })
    data = media if media is not None else (None if payload is None else _json(payload).encode())
    if media is not None:
        headers["Content-Type"] = "image/png"
        headers["X-Mark-Media-Filename"] = "original.png"
    elif data is not None:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["Idempotency-Key"] = key
    request = Request("http://127.0.0.1:" + str(port) + path, data=data, headers=headers, method=method)
    try:
        response = build_opener(ProxyHandler({})).open(request, timeout=8)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(1024 * 1024)
        return response.status, body.decode("utf-8") if raw else json.loads(body), dict(response.headers)


def _wait_server(server, ready, *, close=None):
    stopped = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    managed = hasattr(server, "start")
    thread = None
    if managed:
        server.start()
    else:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
    _emit({"ready": True, "uid": os.geteuid(), **ready})
    stopped.wait(150)
    if managed:
        server.close()
    else:
        server.shutdown()
        if hasattr(server, "wait_for_active_handlers"):
            assert server.wait_for_active_handlers(10)
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()
    if close is not None:
        close()


def _platform(config):
    guard = Lock()
    ads = {"701": {"ad_id": "701", "title": "Existing table", "description": "Existing description", "state": "active"}}
    mutations = []
    modes = {"unknown_next": False, "unreadable": False, "block_next": False,
             "blocked_readers": 0, "media_mismatch_next": False}
    release = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            encoded = _json(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def dispatch(self):
            control = self.path.startswith("/control/")
            token = config["control_token"] if control else config["platform_token"]
            if self.headers.get("Authorization") != "Bearer " + token:
                self.reply(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            assert 0 <= length <= 16384
            payload = json.loads(self.rfile.read(length)) if length else {}
            if control:
                with guard:
                    if self.path == "/control/unknown":
                        modes["unknown_next"] = True
                    elif self.path == "/control/block":
                        modes["block_next"] = True
                    elif self.path == "/control/release":
                        release.set()
                    elif self.path == "/control/media_mismatch":
                        modes["media_mismatch_next"] = True
                    response = {"mutations": list(mutations), "ads": list(ads.values()), "modes": dict(modes)}
                self.reply(200, response)
                return
            if self.command == "GET":
                with guard:
                    parts = self.path.strip("/").split("/")
                    media_result = None
                    if len(parts) == 3 and parts[0] == "ads" and parts[2] == "media":
                        ad = ads.get(parts[1])
                        media_result = None if ad is None else {
                            "ad_id": parts[1], "media_sha256": ad.get("media_sha256", []),
                            "media_sizes": ad.get("media_sizes", []),
                        }
                    blocked = modes["block_next"] and bool(mutations)
                    if blocked:
                        modes["blocked_readers"] += 1
                if blocked and not release.wait(20):
                    self.reply(503, {"error": "synthetic_readback_not_released"})
                    return
                with guard:
                    status = 503 if modes["unreadable"] else 200
                    response = (media_result if media_result is not None else {"ads": list(ads.values())})
                    if media_result is None and len(parts) == 3 and parts[2] == "media":
                        status, response = 404, {"error": "missing_media"}
                self.reply(status, response)
                return
            with guard:
                segments = self.path.strip("/").split("/")
                if self.command == "POST" and segments in (["ads"], ["ads", "media"]):
                    ad_id = str(900 + len(mutations))
                    ads[ad_id] = {"ad_id": ad_id, "title": payload["title"], "description": payload["description"], "state": "active"}
                    if segments == ["ads", "media"]:
                        assert set(payload) == {"title", "description", "media_sha256", "media_sizes"}
                        hashes, sizes = payload["media_sha256"], payload["media_sizes"]
                        assert isinstance(hashes, list) and 1 <= len(hashes) <= 32
                        assert len(hashes) == len(sizes)
                        assert all(isinstance(h, str) and len(h) == 64 for h in hashes)
                        assert all(type(v) is int and v > 0 for v in sizes)
                        ads[ad_id]["media_sha256"] = (
                            ["0" * 64 for _ in hashes] if modes["media_mismatch_next"] else hashes
                        )
                        ads[ad_id]["media_sizes"] = sizes
                        modes["media_mismatch_next"] = False
                else:
                    assert len(segments) in (2, 3) and segments[0] == "ads"
                    ad_id = segments[1]
                    assert ad_id in ads
                    if self.command == "DELETE":
                        del ads[ad_id]
                    elif self.command == "PATCH":
                        assert set(payload) <= {"title", "description"}
                        ads[ad_id].update(payload)
                    elif self.command == "POST" and segments[2] in ("pause", "activate"):
                        ads[ad_id]["state"] = "paused" if segments[2] == "pause" else "active"
                    else:
                        raise AssertionError("unexpected synthetic mutation")
                mutations.append({"method": self.command, "path": self.path, "ad_id": ad_id})
                if modes["unknown_next"]:
                    modes["unreadable"] = True
            self.reply(200, {"accepted": True})

        do_GET = dispatch
        do_POST = dispatch
        do_PATCH = dispatch
        do_DELETE = dispatch

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    _wait_server(server, {"port": server.server_port})


class PlatformAdapter:
    def __init__(self, port, token):
        self.port, self.token = port, token

    def read_ads(self):
        try:
            status, data, _ = _http(self.port, "GET", "/ads", token=self.token)
            if status != 200:
                return ReadResult.failure(ReadStatus.PARSE_ERROR, error="synthetic_postread_unavailable")
            rows = tuple(AdSnapshot(ad_id=row["ad_id"], title=row["title"], description=row["description"],
                                   lifecycle_state=LifecycleState(row["state"]), observed_at=datetime.now(timezone.utc),
                                   source="synthetic-independent-platform") for row in data["ads"])
            return ReadResult.success_nonempty(rows) if rows else ReadResult.success_empty(())
        except (OSError, URLError, ValueError):
            return ReadResult.failure(ReadStatus.PARSE_ERROR, error="synthetic_postread_unavailable")

    def write(self, method, path, payload=None):
        status, _, _ = _http(self.port, method, path, token=self.token, payload=payload)
        assert status == 200

    def create_ad(self, request):
        self.write("POST", "/ads", asdict(request))

    def set_state(self, ad_id, state):
        self.write("POST", "/ads/" + ad_id + ("/pause" if state is LifecycleState.PAUSED else "/activate"))

    def delete_ad(self, ad_id):
        self.write("DELETE", "/ads/" + ad_id)

    def update_content(self, ad_id, *, title=None, description=None):
        self.write("PATCH", "/ads/" + ad_id, {k: v for k, v in (("title", title), ("description", description)) if v is not None})



class SyntheticMediaPage:
    """Owned by the trusted actor UID; exercises the real one-shot media writer.

    The simulated page is only a platform seam: independent platform state is
    held by a different UID, not by this page or by the normal frontend UID.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self.form = PrivateWebCreateSnapshot(
            state=PrivateWebEditorState.READY, title="", description="", price_amount="",
        )
        self.media = None
        self.staged = ()
        self.closed = False
        self.submitted = False

    def open_create_form(self, category_path):
        assert 2 <= len(category_path) <= 6

    def read_create_form(self):
        return self.form

    def replace_create_title(self, value):
        self.form = replace(self.form, title=value)

    def replace_create_description(self, value):
        self.form = replace(self.form, description=value)

    def replace_create_price(self, value):
        self.form = replace(self.form, price_amount=value)

    def submit_create(self):
        raise AssertionError("media page must not use ordinary create submit")

    def stage_create_media(self, files):
        assert not self.staged and files
        self.staged = tuple((Path(f).name, Path(f).read_bytes()) for f in files)
        self.media = PrivateWebCreateMediaSnapshot(
            state=PrivateWebEditorState.READY,
            files=tuple(PrivateWebMediaFileSnapshot(name=name, size_bytes=len(data))
                        for name, data in self.staged),
        )

    def read_create_media(self):
        assert self.media is not None
        return self.media

    def submit_create_media(self, expected):
        assert not self.submitted and expected == self.media and self.staged
        self.submitted = True
        self.adapter.write("POST", "/ads/media", {
            "title": self.form.title, "description": self.form.description,
            "media_sha256": [hashlib.sha256(data).hexdigest() for _, data in self.staged],
            "media_sizes": [len(data) for _, data in self.staged],
        })

    def close(self):
        self.closed = True


class SyntheticMediaVerifier:
    """Checks a separate platform UID's saved bytes identity via fresh readback."""

    def __init__(self, adapter):
        self.adapter = adapter

    def verify_media(self, ad_id, expected_sources, *, timeout_seconds):
        assert 0 < timeout_seconds <= 120
        try:
            status, data, _ = _http(
                self.adapter.port, "GET", "/ads/" + ad_id + "/media",
                token=self.adapter.token,
            )
            if status != 200 or data.get("ad_id") != ad_id:
                return ReadResult.failure(ReadStatus.PARSE_ERROR, error="media_postread_missing")
            actual = data.get("media_sha256")
            sizes = data.get("media_sizes")
            expected = [hashlib.sha256(Path(source.path).read_bytes()).hexdigest()
                        for source in expected_sources]
            expected_sizes = [Path(source.path).stat().st_size for source in expected_sources]
            return ReadResult.success_nonempty(PrivateWebMediaPersistenceSnapshot(
                ad_id=ad_id, observed_at=datetime.now(timezone.utc),
                source="synthetic-independent-platform-media",
                exact_match=actual == expected and sizes == expected_sizes,
            ))
        except (OSError, URLError, ValueError, TypeError):
            return ReadResult.failure(ReadStatus.PARSE_ERROR, error="media_postread_unavailable")


def _child(role, config):
    # One-shot trusted-owner test provisioning runs under the future SQLite
    # owner UID in its OWN disposable process, never through the runtime UID
    # and never during a normal or recovery restart.
    _drop(UIDS["owner"] if role == "owner-bootstrap" else
          UIDS["witness"] if role == "witness-bootstrap" else
          (UIDS[role] if role in UIDS else UIDS["runtime"]))
    if role == "owner-bootstrap":
        db = Path(config["db"])
        assert not db.exists(), "bootstrap must not overwrite an old SQLite identity"
        SnapshotStore(db, create_if_missing=True)
        assert db.is_file() and db.stat().st_uid == UIDS["owner"]
        _emit({"ready": True, "uid": os.geteuid(), "schema_initialized": True})
        return
    if role == "witness-bootstrap":
        # Independent trusted one-shot provisioning: never on a normal
        # restart, and never through the untrusted runtime UID.
        db = Path(config["db"])
        assert not db.exists(), "witness bootstrap must not overwrite a ledger"
        ledger = CompletionWitnessLedger(db, principal=PRINCIPAL, initialize=True)
        ledger.close()
        assert db.is_file() and db.stat().st_uid == UIDS["witness"]
        _emit({"ready": True, "uid": os.geteuid(), "schema_initialized": True})
        return
    if role == "platform":
        _platform(config)
    elif role == "witness":
        if config.get("drop_completion_key"):
            # Synthetic lost-ACK injection is test-only. In all ordinary
            # scenarios the production installed Witness CLI is exercised.
            ledger = CompletionWitnessLedger(
                Path(config["db"]), principal=PRINCIPAL, initialize=False,
            )
            server = CompletionWitnessServer(
                ledger=ledger, owner_socket=Path(config["owner_socket"]),
                authority_socket=Path(config["actor_socket"]),
                owner_uid=UIDS["owner"], owner_gid=UIDS["owner"],
                authority_uid=UIDS["actor"], authority_gid=UIDS["actor"],
                runtime_uid=UIDS["runtime"],
            )
            class DropCompletionAck(_WitnessUnixHandler):
                def _dispatch(self, request, target, session):
                    answer = super()._dispatch(request, target, session)
                    self._drop_once = request.get("op") == "completed" and request.get("key") == config["drop_completion_key"]
                    return answer

                def _send(self, value):
                    if getattr(self, "_drop_once", False):
                        self._drop_once = False
                        self.request.shutdown(socket.SHUT_RDWR)
                        raise ConnectionResetError("synthetic lost completion ACK")
                    super()._send(value)
            server._owner.RequestHandlerClass = DropCompletionAck
            _wait_server(server, {}, close=ledger.close)
        else:
            config_bytes = _json({
                "database": config["db"],
                "owner_socket": config["owner_socket"],
                "actor_socket": config["actor_socket"],
                "owner_uid": UIDS["owner"], "owner_gid": UIDS["owner"],
                "actor_uid": UIDS["actor"], "actor_gid": UIDS["actor"],
                "runtime_uid": UIDS["runtime"], "principal": PRINCIPAL,
            }).encode("utf-8") + b"\n"
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, config_bytes)
            finally:
                os.close(write_fd)
            try:
                os.set_inheritable(read_fd, True)
                cli = Path(sys.executable).with_name("kleinanzeiger-protected-witness")
                assert cli.is_file() and os.access(cli, os.X_OK), "installed witness entry missing"
                os.execv(str(cli), [str(cli), "--config-fd", str(read_fd)])
                raise AssertionError("protected witness exec unexpectedly returned")
            finally:
                os.close(read_fd)
    elif role == "actor":
        adapter = PlatformAdapter(config["platform_port"], config["platform_token"])
        service = MarkService(owner_reader=adapter, management_reader=adapter, delete_confirmation_reader=adapter,
                              reaction_reader=adapter, state_writer=adapter, delete_writer=adapter, content_writer=adapter,
                              create_writer=adapter,
                              content_reader_factory=lambda _: PlatformAdapter(config["platform_port"], config["platform_token"]),
                              store=None, writes_enabled=True)
        witness = AuthorityWitnessClient(Path(config["witness_socket"]), expected_owner_uid=UIDS["witness"])
        handles = None
        media_runtime = None
        media_service = None
        if config.get("media_handles"):
            handles = PrivateWebMediaHandleStore(directory=config["media_handles"])
            media_runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: SyntheticMediaPage(adapter))
            media_service = PrivateWebMediaCreateService(
                runtime=media_runtime, resolver=handles, reader=adapter,
                confirmation_reader=adapter,
                content_reader_factory=lambda _: PlatformAdapter(config["platform_port"], config["platform_token"]),
                media_persistence_verifier=SyntheticMediaVerifier(adapter),
                store=None, writes_enabled=True,
            )
        server = CompletionAuthorityServer(socket_path=Path(config["socket"]), owner_uid=UIDS["owner"], owner_gid=UIDS["owner"],
                                           runtime_uid=UIDS["runtime"], principal=PRINCIPAL, service=service,
                                           witness_client=witness, media_service=media_service, media_stager=handles)
        def close_actor():
            try:
                if media_runtime is not None:
                    media_runtime.close()
            finally:
                if handles is not None:
                    handles.close()
        _wait_server(server, {}, close=close_actor)
    elif role == "owner":
        # Exercise the installed wheel's real console entry under the owner UID.
        # No test-only factory or implicit SQLite creation on restart.
        config_bytes = _json({
            "database": config["db"],
            "dashboard_socket": config["dashboard_socket"],
            "witness_socket": config["witness_socket"],
            "actor_socket": config["actor_socket"],
            "witness_uid": UIDS["witness"], "actor_uid": UIDS["actor"],
            "runtime_uid": UIDS["runtime"], "runtime_gid": UIDS["runtime"],
            "principal": PRINCIPAL, "bearer_token": config["api_token"],
            "media_enabled": bool(config.get("media_enabled")), "write_port": 0,
            "sync_interval_seconds": config.get("sync_interval_seconds", 0),
        }).encode("utf-8") + b"\n"
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, config_bytes)
        finally:
            os.close(write_fd)
        try:
            # This descriptor is the only deliberately inherited private input.
            os.set_inheritable(read_fd, True)
            cli = Path(sys.executable).with_name("kleinanzeiger-protected-owner")
            assert cli.is_file() and os.access(cli, os.X_OK), "installed owner entry missing"
            os.execv(str(cli), [str(cli), "--config-fd", str(read_fd)])
            raise AssertionError("protected owner exec unexpectedly returned")
        finally:
            os.close(read_fd)
    elif role == "frontend":
        # Use the *installed* executable product path, not a test-only server
        # factory. Pass tokens via a disposable private pipe, never argv/logs.
        from mark_api.protected_frontend import main as frontend_main
        private_config = {
            "owner_socket": config["dashboard_socket"], "owner_uid": UIDS["owner"],
            "principal": PRINCIPAL, "owner_port": config["owner_port"],
            "api_token": config["api_token"], "ui_token": config["ui_token"],
            "dashboard_port": 0,
        }
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, (_json(private_config) + "\n").encode("utf-8"))
        finally:
            os.close(write_fd)
        try:
            assert frontend_main(["--config-fd", str(read_fd)]) == 0
        finally:
            os.close(read_fd)
    elif role == "request":
        result = _http(config["port"], config["method"], config["path"], token=config["api_token"],
                       payload=config.get("payload"), key=config.get("key"),
                       ui_token=config.get("ui_token"), raw=config.get("raw", False),
                       media=(b64decode(config["media_b64"], validate=True) if config.get("media_b64") else None))
        _emit({"status": result[0], "body": result[1], "replayed": result[2].get("Idempotency-Replayed")})
    elif role == "probe":
        for path in config["files"]:
            try:
                descriptor = os.open(path, os.O_RDWR)
            except PermissionError:
                continue
            else:
                os.close(descriptor)
                raise AssertionError("untrusted UID opened protected resource")
        for path in config["sockets"]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(2)
                try:
                    peer.connect(path)
                    peer.sendall(b'{"version":1,"op":"operator_clear","key":"forged","sha":"0000000000000000000000000000000000000000000000000000000000000000","evidence":"forged"}\n')
                    response = peer.recv(1024)
                except (PermissionError, ConnectionResetError, BrokenPipeError):
                    continue
                assert not response or json.loads(response).get("ok") is False
        for directory in config.get("private_dirs", []):
            try:
                os.listdir(directory)
            except PermissionError:
                continue
            raise AssertionError("untrusted UID listed actor-private media")
        _emit({"denied": True, "uid": os.geteuid(), "file_count": len(config["files"]),
               "socket_count": len(config["sockets"]), "private_dirs_denied": len(config.get("private_dirs", []))})
    else:
        raise AssertionError("unknown child role")


class Scenario:
    def __init__(self, root, *, lost_ack=False, dashboard=False, media=False, sync_interval_seconds=0):
        self.root = root
        self.children = []
        self.configs = {}
        self.procs = {}
        self.api_token = secrets.token_hex(24)
        self.ui_token = secrets.token_hex(24)
        self.with_dashboard = dashboard
        self.with_media = media
        self.sync_interval_seconds = sync_interval_seconds
        self.platform_token = secrets.token_hex(24)
        self.control_token = secrets.token_hex(24)
        root.mkdir(mode=0o755)
        # An enforcing supervisor may have umask 077. The disposable root
        # must be traversable by the independent synthetic child UIDs.
        os.chmod(root, 0o755)
        self.owner_db = root / "owner-data" / "snapshot.sqlite"
        self.witness_db = root / "witness-data" / "witness.sqlite"
        self.owner_witness_socket = root / "w-owner" / "w.sock"
        self.actor_witness_socket = root / "w-actor" / "w.sock"
        self.actor_socket = root / "actor-rpc" / "a.sock"
        self.dashboard_socket = root / "dashboard-rpc" / "d.sock"
        self.actor_media_root = root / "actor-media"
        directories = [
            (self.owner_db.parent, UIDS["owner"], UIDS["owner"], 0o700),
            (self.witness_db.parent, UIDS["witness"], UIDS["witness"], 0o700),
            (self.owner_witness_socket.parent, UIDS["witness"], UIDS["owner"], 0o2710),
            (self.actor_witness_socket.parent, UIDS["witness"], UIDS["actor"], 0o2710),
            (self.actor_socket.parent, UIDS["actor"], UIDS["owner"], 0o2710),
            (self.dashboard_socket.parent, UIDS["owner"], UIDS["runtime"], 0o2710),
        ]
        if media:
            directories.append((self.actor_media_root, UIDS["actor"], UIDS["actor"], 0o700))
        for path, uid, gid, mode in directories:
            path.mkdir()
            os.chown(path, uid, gid)
            os.chmod(path, mode)
        try:
            bootstrap = self.spawn("owner-bootstrap", {"db": str(self.owner_db)})
            ready = self.receive(bootstrap)
            bootstrap.wait(5)
            assert bootstrap.returncode == 0 and ready == {
                "ready": True, "uid": UIDS["owner"], "schema_initialized": True,
            }, ready
            witness_bootstrap = self.spawn("witness-bootstrap", {"db": str(self.witness_db)})
            witness_ready = self.receive(witness_bootstrap)
            witness_bootstrap.wait(5)
            assert witness_bootstrap.returncode == 0 and witness_ready == {
                "ready": True, "uid": UIDS["witness"], "schema_initialized": True,
            }, witness_ready
            self.platform_port = self.start("platform", {"platform_token": self.platform_token, "control_token": self.control_token})["port"]
            self.start("witness", {"db": str(self.witness_db), "owner_socket": str(self.owner_witness_socket),
                                   "actor_socket": str(self.actor_witness_socket), "drop_completion_key": "lost:pause" if lost_ack else None})
            self.start("actor", {"socket": str(self.actor_socket), "witness_socket": str(self.actor_witness_socket),
                                 "platform_port": self.platform_port, "platform_token": self.platform_token,
                                 "media_handles": str(self.actor_media_root / "handles") if media else None})
            self.api_port = self.start("owner", {"db": str(self.owner_db), "witness_socket": str(self.owner_witness_socket),
                                                 "actor_socket": str(self.actor_socket), "api_token": self.api_token,
                                                 "dashboard_socket": str(self.dashboard_socket),
                                                 "media_enabled": media,
                                                 "sync_interval_seconds": sync_interval_seconds})["port"]
            if dashboard:
                self.start_frontend()
        except BaseException:
            self.close()
            raise

    def spawn(self, role, config):
        # Private pipe, never argv/environ. exec prevents an untrusted child
        # from inheriting supervisor memory containing other role credentials.
        child = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--child", role],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, close_fds=True,
                                 env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        self.children.append(child)
        child.stdin.write(_json(config) + "\n")
        child.stdin.close()
        return child

    def receive(self, child, timeout=25):
        if not select.select([child.stdout], [], [], timeout)[0]:
            raise AssertionError("bounded synthetic child response missing")
        line = child.stdout.readline()
        if not line:
            error = child.stderr.read(8192) if child.poll() is not None else "child exited before readiness"
            raise AssertionError(error)
        return json.loads(line)

    def start(self, role, config):
        self.configs[role] = config
        child = self.spawn(role, config)
        self.procs[role] = child
        ready = self.receive(child)
        assert ready["ready"] is True and ready["uid"] == UIDS.get(role, UIDS["runtime"])
        return ready

    def stop(self, role, *, kill=False, already_signalled=False):
        assert not (kill and already_signalled)
        child = self.procs.pop(role)
        if child.poll() is None and not already_signalled:
            child.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
        try:
            child.wait(15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(5)
            raise AssertionError("child shutdown did not drain")
        if not kill:
            assert child.returncode == 0, (role, child.returncode, child.stderr.read(8192))

    def restart_owner(self):
        if "frontend" in self.procs:
            self.stop("frontend")
        if "owner" in self.procs:
            self.stop("owner")
        deadline = time.monotonic() + 5
        while True:
            with closing(sqlite3.connect("file:" + str(self.witness_db) + "?mode=ro", uri=True)) as conn:
                active = conn.execute("SELECT count(*) FROM sessions WHERE state='active'").fetchone()[0]
            if active == 0:
                break
            assert time.monotonic() < deadline
            time.sleep(0.02)
        self.api_port = self.start("owner", self.configs["owner"])["port"]
        if self.with_dashboard:
            self.start_frontend()

    def start_frontend(self):
        self.ui_port = self.start("frontend", {
            "dashboard_socket": str(self.dashboard_socket), "owner_port": self.api_port,
            "api_token": self.api_token, "ui_token": self.ui_token,
        })["port"]

    def restart_frontend(self):
        self.stop("frontend")
        self.start_frontend()

    def ui_request(self, method, path, key=None, payload=None, *, raw=False, media=None):
        assert self.with_dashboard
        child = self.spawn("request", {"port": self.ui_port, "api_token": "", "ui_token": self.ui_token,
                                       "method": method, "path": path, "key": key, "payload": payload,
                                       "raw": raw, "media_b64": b64encode(media).decode("ascii") if media is not None else None})
        result = self.receive(child)
        child.wait(5)
        assert child.returncode == 0, child.stderr.read(8192)
        return result

    def ui_ack(self, scope, key):
        return self.ui_request("POST", "/api/dashboard/pending-writes/ack", payload={"scope": scope, "idempotency_key": key})

    def request(self, method, path, key=None, payload=None, *, media=None):
        child = self.spawn("request", {"port": self.api_port, "api_token": self.api_token,
                                       "method": method, "path": path, "key": key, "payload": payload,
                                       "media_b64": b64encode(media).decode("ascii") if media is not None else None})
        result = self.receive(child)
        child.wait(5)
        assert child.returncode == 0, child.stderr.read(8192)
        return result

    def state(self, mode="state"):
        status, data, _ = _http(self.platform_port, "GET", "/control/" + mode, token=self.control_token)
        assert status == 200
        return data

    def probe(self, *, peer_only=False):
        # Owner always exposes the separately UID-attested dashboard RPC,
        # even in HTTP-only scenarios. Probe that endpoint on every run.
        paths = [self.actor_socket, self.actor_witness_socket,
                 self.owner_witness_socket, self.dashboard_socket]
        old = []
        if peer_only:
            # Deliberately expose ONLY disposable test sockets: rejection must
            # come from SO_PEERCRED, not merely from pathname access controls.
            for path in paths:
                old.append((path.parent, stat.S_IMODE(path.parent.stat().st_mode)))
                old.append((path, stat.S_IMODE(path.stat().st_mode)))
                os.chmod(path.parent, 0o755)
                os.chmod(path, 0o666)
        try:
            files = [str(self.owner_db) + suffix for suffix in ("", "-wal", "-shm")] + [str(self.witness_db)]
            child = self.spawn("probe", {
                "files": files, "sockets": list(map(str, paths)),
                "private_dirs": [str(self.actor_media_root)] if self.with_media else [],
            })
            result = self.receive(child)
            child.wait(5)
            assert child.returncode == 0 and result["denied"] is True
        finally:
            for path, mode in reversed(old):
                os.chmod(path, mode)

    def dashboard_journey(self):
        page = self.ui_request("GET", "/", raw=True)
        assert page["status"] == 200 and "<html" in page["body"].lower()
        assert self.api_token not in page["body"]
        for path in ("/api/summary", "/api/ads", "/api/email-reactions", "/api/sync/status",
                     "/api/sync/recovery", "/api/analytics/metrics", "/api/analytics/contract"):
            result = self.ui_request("GET", path)
            assert result["status"] == 200, (path, result)
        self.probe(peer_only=True)
        payload = {"category_path": ["Haus & Garten", "Möbel & Wohnen"], "title": "UI synthetic table",
                   "description": "Protected UI description", "price_eur": 25}
        created = self.ui_request("POST", "/api/write/ads", "ui:create", payload)
        assert created["status"] == 200, created
        ad_id = created["body"]["operation_receipt"]["created_ad_id"]
        operations = [("POST", "/api/write/ads", "ui:create", payload, "create"),
                      ("PATCH", "/api/write/ads/" + ad_id, "ui:update", {"title": "Protected changed title"}, "ad:" + ad_id + ":update"),
                      ("POST", "/api/write/ads/" + ad_id + "/pause", "ui:pause", None, "ad:" + ad_id + ":pause"),
                      ("POST", "/api/write/ads/" + ad_id + "/activate", "ui:activate", None, "ad:" + ad_id + ":activate"),
                      ("DELETE", "/api/write/ads/" + ad_id, "ui:delete", None, "ad:" + ad_id + ":delete")]
        for count, (method, path, key, body, scope) in enumerate(operations, 1):
            result = self.ui_request(method, path, key, body)
            assert result["status"] == 200 and result["body"]["platform_retry_authorized"] is False, result
            pending = self.ui_request("GET", "/api/dashboard/pending-writes")
            assert pending["status"] == 200 and len(pending["body"]["pending_writes"]) == 1, pending
            assert pending["body"]["pending_writes"][0]["key"] == key
            self.restart_frontend()
            replay = self.ui_request(method, path, key, body)
            assert replay["status"] == 200 and replay["replayed"] == "true", replay
            phase1 = self.ui_ack(scope, key)
            assert phase1["status"] == 200 and phase1["body"] == {"acknowledged": True, "finalized": False}, phase1
            self.restart_owner()  # Durable first ACK survives both process restarts.
            phase2 = self.ui_ack(scope, key)
            assert phase2["status"] == 200 and phase2["body"] == {"acknowledged": True, "finalized": True}, phase2
            assert self.ui_request("GET", "/api/dashboard/pending-writes")["body"] == {"pending_writes": []}
            replay = self.ui_request(method, path, key, body)
            assert replay["status"] == 200 and replay["replayed"] == "true", replay
            assert self.ui_ack(scope, key)["status"] in (409, 500)
            assert len(self.state()["mutations"]) == count
        self.probe()

    def media_journey(self):
        assert self.with_media and self.with_dashboard
        stage = self.ui_request("POST", "/api/write/media/stage", media=MEDIA_PNG)
        assert stage["status"] == 201, stage
        ref = stage["body"]["media_ref"]
        assert ref.startswith("media_") and len(ref) > 18
        # This stage was held exclusively under the actor UID, not the owner
        # SQLite UID or the compromised dashboard/runtime UID.
        self.probe(peer_only=True)
        payload = {
            "category_path": ["Haus & Garten", "Möbel & Wohnen"],
            "title": "UID synthetic image listing",
            "description": "Separate actor, witness and platform media readback",
            "price_eur": 29, "media_refs": [ref],
        }
        key = "ui:protected-media"
        created = self.ui_request("POST", "/api/write/media/ads", key, payload)
        assert created["status"] == 200, created
        assert created["body"]["media_persistence_confirmed"] is True, created
        assert created["body"]["media_post_read_status"] == "confirmed", created
        assert created["body"]["operation_receipt"]["media_persistence_confirmed"] is True, created
        assert ref not in _json(created["body"]), "private media ref leaked in write response"
        state = self.state()
        assert len(state["mutations"]) == 1 and state["mutations"][0]["path"] == "/ads/media", state
        created_id = created["body"]["operation_receipt"]["created_ad_id"]
        matching = [ad for ad in state["ads"] if ad["ad_id"] == created_id]
        assert len(matching) == 1
        assert matching[0]["media_sha256"] == [hashlib.sha256(MEDIA_PNG).hexdigest()]
        assert matching[0]["media_sizes"] == [len(MEDIA_PNG)]
        pending = self.ui_request("GET", "/api/dashboard/pending-writes")
        assert pending["status"] == 200 and len(pending["body"]["pending_writes"]) == 1, pending
        assert pending["body"]["pending_writes"][0]["scope"] == "create-media", pending
        assert pending["body"]["pending_writes"][0]["acknowledged"] is False
        self.restart_frontend()
        replay = self.ui_request("POST", "/api/write/media/ads", key, payload)
        assert replay["status"] == 200 and replay["replayed"] == "true", replay
        phase1 = self.ui_ack("create-media", key)
        assert phase1["status"] == 200 and phase1["body"]["finalized"] is False, phase1
        self.restart_owner()
        phase2 = self.ui_ack("create-media", key)
        assert phase2["status"] == 200 and phase2["body"]["finalized"] is True, phase2
        assert self.ui_request("GET", "/api/dashboard/pending-writes")["body"] == {"pending_writes": []}
        # Actor process restart must not become new actuation authority.
        self.stop("actor")
        self.start("actor", self.configs["actor"])
        replay = self.ui_request("POST", "/api/write/media/ads", key, payload)
        assert replay["status"] == 200 and replay["replayed"] == "true", replay
        assert len(self.state()["mutations"]) == 1
        self.probe()

    def media_unverified_journey(self):
        assert self.with_media and self.with_dashboard
        stage = self.ui_request("POST", "/api/write/media/stage", media=MEDIA_PNG)
        assert stage["status"] == 201, stage
        payload = {
            "category_path": ["Haus & Garten", "Möbel & Wohnen"],
            "title": "UID synthetic unverified listing",
            "description": "Content can match while media postread differs",
            "price_eur": 29, "media_refs": [stage["body"]["media_ref"]],
        }
        self.state("media_mismatch")
        key = "ui:media-unverified"
        result = self.ui_request("POST", "/api/write/media/ads", key, payload)
        assert result["status"] in (202, 500, 503), result
        assert result["body"]["platform_retry_authorized"] is False, result
        assert len(self.state()["mutations"]) == 1
        self.restart_owner()
        for attempted_key in (key, key + ":new"):
            denied = self.ui_request("POST", "/api/write/media/ads", attempted_key, payload)
            assert denied["status"] in (409, 500, 503), denied
            assert denied["body"]["platform_retry_authorized"] is False, denied
        assert self.ui_ack("create-media", key)["status"] in (409, 500)
        pending = self.ui_request("GET", "/api/dashboard/pending-writes")
        assert len(pending["body"]["pending_writes"]) == 1, pending
        assert pending["body"]["pending_writes"][0]["acknowledged"] is False
        assert len(self.state()["mutations"]) == 1
        self.probe(peer_only=True)

    def crash_after_submit(self, role, *, graceful=False, media=False):
        key = "crash:" + role + (":term" if graceful else ":kill") + (":media" if media else "")
        path = "/api/write/ads/701/pause"
        payload = None
        if media:
            assert self.with_media
            staged = self.request("POST", "/api/write/media/stage", media=MEDIA_PNG)
            assert staged["status"] == 201, staged
            path = "/api/write/media/ads"
            payload = {
                "category_path": ["Haus & Garten", "Möbel & Wohnen"],
                "title": "UID media crash proof",
                "description": "One-shot media input before actor or owner death",
                "price_eur": 29, "media_refs": [staged["body"]["media_ref"]],
            }
        self.state("block")
        child = self.spawn("request", {"port": self.api_port, "api_token": self.api_token,
                                       "method": "POST", "path": path, "key": key, "payload": payload})
        deadline = time.monotonic() + 8
        while True:
            evidence = self.state()
            if len(evidence["mutations"]) == 1 and evidence["modes"]["blocked_readers"] > 0:
                break
            assert time.monotonic() < deadline, "no exact post-submit crash window"
            time.sleep(0.02)
        # The synthetic platform has durably changed state; its independent
        # readback is still held. Signal ONLY the exact child created above.
        if graceful:
            self.procs[role].send_signal(signal.SIGTERM)
            self.state("release")
            result = self.receive(child)
            child.wait(5)
            assert child.returncode == 0 and result["status"] in (200, 202, 503), result
            # This owner already received SIGTERM. A second signal during
            # interpreter teardown can change the observed exit status on 3.13.
            self.stop(role, already_signalled=True)
        else:
            # Pin exactly the disposable socket inode before SIGKILL.
            # Only after waitpid proves its creator has exited may this
            # synthetic supervisor retire a stale UDS pathname.
            socket_path = self.actor_socket if role == "actor" else self.dashboard_socket
            socket_identity = socket_path.stat()
            self.stop(role, kill=True)
            self.state("release")
            child.wait(10)
            if role == "actor":
                result = self.receive(child)
                assert result["status"] in (500, 503), result
                # A killed server cannot unlink its private stale socket. The
                # isolated supervisor retires only that exact observed inode,
                # after waitpid proved its creator is dead. No host path touched.
                current = self.actor_socket.lstat()
                assert stat.S_ISSOCK(current.st_mode) and current.st_uid == UIDS["actor"]
                assert (current.st_dev, current.st_ino) == (socket_identity.st_dev, socket_identity.st_ino)
                self.actor_socket.unlink()
                self.start("actor", self.configs["actor"])
            else:
                assert child.returncode != 0, "killed owner unexpectedly returned success"
                current = self.dashboard_socket.lstat()
                assert stat.S_ISSOCK(current.st_mode) and current.st_uid == UIDS["owner"]
                assert (current.st_dev, current.st_ino) == (
                    socket_identity.st_dev, socket_identity.st_ino,
                )
                # This is test-only after an exact child waitpid, never a
                # production host-FD/MAP_SHARED/restore clearance.
                self.dashboard_socket.unlink()
        self.restart_owner()
        replay = self.request("POST", path, key, payload)
        assert replay["body"]["platform_retry_authorized"] is False, replay
        if graceful and result["status"] == 200:
            assert replay["status"] == 200 and replay["replayed"] == "true", replay
        else:
            assert replay["status"] in (409, 500, 503), replay
            another = self.request("POST", path, key + ":new", payload)
            assert another["status"] in (409, 500, 503), another
        assert len(self.state()["mutations"]) == 1
        self.probe(peer_only=True)

    def close(self):
        errors = []
        try:
            for role in ("frontend", "owner", "actor", "witness", "platform"):
                if role in self.procs:
                    try:
                        self.stop(role)
                    except BaseException as exc:
                        errors.append(type(exc).__name__)
        finally:
            for child in self.children:
                if child.poll() is None:
                    child.kill()
                    child.wait(5)
                child.stdout.close()
                child.stderr.close()
        if errors:
            raise AssertionError("synthetic role shutdown failed: " + ",".join(errors))


def _acceptance(host_net, host_pid):
    assert sys.platform == "linux" and os.geteuid() == 0 and os.getpid() == 1
    assert os.readlink("/proc/self/ns/net") != host_net and os.readlink("/proc/self/ns/pid") != host_pid
    interfaces = [line.split(":")[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:]]
    assert interfaces == ["lo"], interfaces
    package = Path(mark_api.__file__).resolve()
    assert "site-packages" in package.parts and package.stat().st_uid == 0, "requires root-owned installed wheel, never checkout imports"
    with tempfile.TemporaryDirectory(prefix="a6-uid-acceptance-") as temp:
        root = Path(temp)
        os.chmod(root, 0o755)
        scenario = None
        try:
            scenario = Scenario(root / "crud")
            scenario.probe()
            scenario.probe(peer_only=True)
            payload = {"category_path": ["Haus & Garten", "Möbel & Wohnen"], "title": "Synthetic table", "description": "Synthetic description", "price_eur": 25}
            created = scenario.request("POST", "/api/write/ads", "crud:create", payload)
            assert created["status"] == 200, created
            ad_id = created["body"]["operation_receipt"]["created_ad_id"]
            operations = [("POST", "/api/write/ads", "crud:create", payload),
                          ("PATCH", "/api/write/ads/" + ad_id, "crud:update", {"title": "Updated table", "description": "Updated description"}),
                          ("POST", "/api/write/ads/" + ad_id + "/pause", "crud:pause", None),
                          ("POST", "/api/write/ads/" + ad_id + "/activate", "crud:activate", None),
                          ("DELETE", "/api/write/ads/" + ad_id, "crud:delete", None)]
            for expected, operation in enumerate(operations, 1):
                result = scenario.request(*operation)
                assert result["status"] == 200 and result["body"]["platform_retry_authorized"] is False, result
                replay = scenario.request(*operation)
                assert replay["status"] == 200 and replay["replayed"] == "true", replay
                assert len(scenario.state()["mutations"]) == expected
            scenario.restart_owner()
            assert scenario.request(*operations[0])["status"] == 200
            assert len(scenario.state()["mutations"]) == 5
            scenario.close()
            scenario = Scenario(root / "protected-inventory", dashboard=True, sync_interval_seconds=60)
            deadline = time.monotonic() + 12
            while True:
                # Read-only supervisor observation. The data itself came from
                # installed Owner CLI -> independent Actor UID -> platform UID.
                with closing(sqlite3.connect(
                    "file:" + str(scenario.owner_db) + "?mode=ro", uri=True, timeout=2,
                )) as conn:
                    observed = conn.execute(
                        "SELECT outcome,snapshot_count FROM sync_attempts "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    current = conn.execute(
                        "SELECT title FROM ad_snapshots WHERE ad_id='701' "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                if observed == ("success_nonempty", 1) and current == ("Existing table",):
                    break
                assert time.monotonic() < deadline, (observed, current)
                time.sleep(0.05)
            assert scenario.ui_request("GET", "/api/sync/status")["status"] == 200
            assert scenario.ui_request("GET", "/api/ads")["status"] == 200
            assert scenario.state()["mutations"] == [], "inventory must never mutate platform"
            scenario.probe(peer_only=True)
            scenario.close()
            scenario = Scenario(root / "lost-ack", lost_ack=True)
            lost = scenario.request("POST", "/api/write/ads/701/pause", "lost:pause")
            assert lost["status"] == 503 and lost["body"]["platform_retry_authorized"] is False, lost
            assert len(scenario.state()["mutations"]) == 1
            scenario.restart_owner()
            replay = scenario.request("POST", "/api/write/ads/701/pause", "lost:pause")
            assert replay["status"] == 200 and replay["replayed"] == "true", replay
            assert len(scenario.state()["mutations"]) == 1
            scenario.close()
            scenario = Scenario(root / "unknown")
            scenario.state("unknown")
            unknown = scenario.request("POST", "/api/write/ads/701/pause", "unknown:pause")
            assert unknown["status"] == 202 and unknown["body"]["platform_retry_authorized"] is False, unknown
            scenario.restart_owner()
            for key in ("unknown:pause", "unknown:new-key"):
                blocked = scenario.request("POST", "/api/write/ads/701/pause", key)
                assert blocked["status"] in (409, 500, 503) and blocked["body"]["platform_retry_authorized"] is False, blocked
            assert len(scenario.state()["mutations"]) == 1
            scenario.probe(peer_only=True)
            scenario.close()
            scenario = None
            for role, graceful in (("actor", False), ("owner", False), ("owner", True)):
                scenario = Scenario(root / ("crash-" + role + ("-term" if graceful else "-kill")))
                scenario.crash_after_submit(role, graceful=graceful)
                scenario.close()
                scenario = None
            for role, graceful in (("actor", False), ("owner", False), ("owner", True)):
                scenario = Scenario(root / ("media-crash-" + role + ("-term" if graceful else "-kill")), media=True)
                scenario.crash_after_submit(role, graceful=graceful, media=True)
                scenario.close()
                scenario = None
            scenario = Scenario(root / "media-dashboard", dashboard=True, media=True)
            scenario.media_journey()
            scenario.close()
            scenario = Scenario(root / "media-unverified", dashboard=True, media=True)
            scenario.media_unverified_journey()
            scenario.close()
            scenario = Scenario(root / "dashboard", dashboard=True)
            scenario.dashboard_journey()
            scenario.close()
            scenario = Scenario(root / "dashboard-unknown", dashboard=True)
            scenario.state("unknown")
            unknown = scenario.ui_request("POST", "/api/write/ads/701/pause", "ui:unknown")
            assert unknown["status"] == 202, unknown
            scenario.restart_owner()
            for attempt in range(2):
                refused = scenario.ui_ack("ad:701:pause", "ui:unknown")
                assert refused["status"] in (409, 500), refused
                pending = scenario.ui_request("GET", "/api/dashboard/pending-writes")
                assert len(pending["body"]["pending_writes"]) == 1 and pending["body"]["pending_writes"][0]["acknowledged"] is False
            denied_replay = scenario.ui_request("POST", "/api/write/ads/701/pause", "ui:unknown")
            assert denied_replay["status"] in (409, 500), denied_replay
            assert len(scenario.state()["mutations"]) == 1
            scenario.probe(peer_only=True)
            scenario.close()
            scenario = Scenario(root / "dashboard-lost-ack", dashboard=True, lost_ack=True)
            lost = scenario.ui_request("POST", "/api/write/ads/701/pause", "lost:pause")
            assert lost["status"] == 503, lost
            scenario.restart_owner()
            replay = scenario.ui_request("POST", "/api/write/ads/701/pause", "lost:pause")
            assert replay["status"] == 200 and replay["replayed"] == "true", replay
            assert scenario.ui_ack("ad:701:pause", "lost:pause")["status"] == 200
            assert scenario.ui_ack("ad:701:pause", "lost:pause")["body"]["finalized"] is True
            assert len(scenario.state()["mutations"]) == 1
            scenario.close()
            scenario = None
        finally:
            if scenario is not None:
                scenario.close()
    hashes = {name: hashlib.sha256((package.parent / name).read_bytes()).hexdigest()
              for name in ("completion_authority.py", "completion_witness.py", "fence_witness.py", "completion_dashboard.py", "dashboard.py")}
    _emit({"result": "PASS", "scope": "installed-a6-independent-media-nonmedia-uid-composition", "roles": UIDS,
           "installed_witness_cli": True, "protected_actor_inventory_sync": True,
           "crud": True, "restart_replay": True, "lost_completion_ack": True, "unknown_fenced": True,
           "runtime_raw_db_wal_shm_denied": True, "forged_completion_clearance_denied": True,
           "kernel_peer_credentials": True, "loopback_only": True, "platform_access": False,
           "sigkill_actor_after_submit": True, "sigkill_owner_after_submit": True,
           "sigterm_owner_inflight_drained": True,
           "sigkill_media_actor_after_submit": True, "sigkill_media_owner_after_submit": True,
           "sigterm_media_owner_inflight_drained": True,
           "protected_media_create": True, "protected_media_private_upload": True,
           "protected_media_independent_postread": True, "protected_media_restart_replay": True,
           "protected_media_two_phase_ack": True, "protected_media_unknown_fenced": True,
           "protected_dashboard_crud": True, "protected_dashboard_two_phase_ack": True,
           "protected_dashboard_unknown_fenced": True, "protected_dashboard_lost_ack_replay": True,
           "host_cutover_attested": False, "media_release_attested": False, "source_sha256": hashes})


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--child":
            config = json.loads(sys.stdin.readline(32768))
            sys.stdin.close()
            _child(sys.argv[2], config)
        else:
            parser = argparse.ArgumentParser()
            parser.add_argument("--host-net", required=True)
            parser.add_argument("--host-pid", required=True)
            arguments = parser.parse_args()
            _acceptance(arguments.host_net, arguments.host_pid)
    except BaseException:
        traceback.print_exc()
        raise SystemExit(1)
