"""Protected product actor admission and lifecycle regressions.

Unit mocks are scoped to wiring tests. They do not replace installed
multi-UID, CDP, external-media or host-cutover acceptance.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

from mark_api.completion_authority import CompletionAuthorityError
from mark_api.protected_actor import (
    ProtectedActorRuntime, _attest_witness_socket,
    _read_private_actor_config, create_protected_actor_runtime, main,
)


def parameters():
    uid = os.geteuid()
    return {
        "socket": "/tmp/ka-actor-synthetic.sock",
        "witness_socket": "/tmp/ka-actor-witness-synthetic.sock",
        "owner_uid": uid + 101, "owner_gid": uid + 101,
        "witness_uid": uid + 102, "runtime_uid": uid + 103,
        "principal": "synthetic-actor-customer",
        "cdp_port": 9222, "timeout_seconds": 5.0,
        "media_enabled": False, "media_directory": None,
    }


class PrivateActorConfigTests(unittest.TestCase):
    def pipe(self, value: bytes) -> int:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, value)
        finally:
            os.close(write_fd)
        self.addCleanup(os.close, read_fd)
        return read_fd

    def test_strict_bounded_private_config(self):
        cfg = parameters()
        got = _read_private_actor_config(self.pipe(json.dumps(cfg).encode() + b"\n"))
        self.assertEqual(got, cfg)

    def test_invalid_native_types_paths_and_unknown_fields_are_denied(self):
        cfg = parameters()
        cases = [
            {**cfg, "cdp_port": True},
            {**cfg, "owner_uid": False},
            {**cfg, "timeout_seconds": float("nan")},
            {**cfg, "timeout_seconds": 31},
            {**cfg, "socket": "/tmp/../tmp/a.sock"},
            {**cfg, "witness_socket": cfg["socket"]},
            {**cfg, "media_enabled": 1},
            {**cfg, "media_directory": "/tmp/stale-media"},
            {**cfg, "media_enabled": True, "media_directory": None},
            {**cfg, "injected": "sqlite:any"},
            {k: v for k, v in cfg.items() if k != "witness_uid"},
        ]
        for variant in cases:
            with self.subTest(variant=repr(variant)[:75]), self.assertRaises(ValueError):
                _read_private_actor_config(self.pipe(json.dumps(variant).encode() + b"\n"))
        for value in (b'{"cdp_port":9222,"cdp_port":9223}\n', b"{}\n" + b"x" * 8200):
            with self.assertRaises(ValueError):
                _read_private_actor_config(self.pipe(value))

    def test_missing_config_denied_without_secret_or_paths_in_stderr(self):
        secret_path = "/tmp/synthetic-actor-private-token-do-not-leak"
        cfg = {**parameters(), "witness_socket": secret_path}
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = main(["--config-fd", str(self.pipe((json.dumps(cfg)+"\n").encode()))])
        self.assertEqual(rc, 2)
        self.assertIn("unavailable", buf.getvalue())
        self.assertNotIn(secret_path, buf.getvalue())
        self.assertNotIn(cfg["principal"], buf.getvalue())


class ActorCompositionTests(unittest.TestCase):
    def test_owner_actor_same_uid_or_overlapping_witness_denied_before_browser(self):
        defaults = parameters()
        for altered in (
            {"owner_uid": os.geteuid()},
            {"runtime_uid": os.geteuid()},
            {"witness_uid": defaults["owner_uid"]},
            {"cdp_port": True},
            {"cdp_port": 0},
            {"socket_path": Path("relative.sock")},
            {"media_directory": Path("/tmp/synthetic-not-enabled")},
        ):
            cfg = {
                "socket_path": Path(defaults["socket"]),
                "witness_socket": Path(defaults["witness_socket"]),
                "owner_uid": defaults["owner_uid"], "owner_gid": defaults["owner_gid"],
                "witness_uid": defaults["witness_uid"],
                "runtime_uid": defaults["runtime_uid"],
                "principal": defaults["principal"], "cdp_port": defaults["cdp_port"],
                "timeout_seconds": defaults["timeout_seconds"],
                "media_enabled": False, "media_directory": None,
                **altered,
            }
            with self.subTest(changed=altered), patch(
                "mark_api.protected_actor.build_protected_actor_content_runtime"
            ) as browser:
                with self.assertRaises((ValueError, CompletionAuthorityError)):
                    create_protected_actor_runtime(**cfg)
                browser.assert_not_called()

    def test_no_witness_never_starts_browser_or_actor(self):
        cfg = parameters()
        with patch(
            "mark_api.protected_actor.build_protected_actor_content_runtime"
        ) as content:
            with self.assertRaises(ValueError):
                create_protected_actor_runtime(
                    socket_path=Path(cfg["socket"]),
                    witness_socket=Path(cfg["witness_socket"]),
                    owner_uid=cfg["owner_uid"], owner_gid=cfg["owner_gid"],
                    witness_uid=cfg["witness_uid"], runtime_uid=cfg["runtime_uid"],
                    principal=cfg["principal"], cdp_port=cfg["cdp_port"],
                    media_enabled=False,
                )
            content.assert_not_called()

    @unittest.skipIf(os.geteuid() == 0, "actor must have nonroot Linux UID")
    def test_real_product_service_wiring_without_sqlite_and_without_media(self):
        cfg = parameters()
        content = MagicMock()
        with patch("mark_api.protected_actor._attest_witness_socket"), patch(
            "mark_api.protected_actor.build_protected_actor_content_runtime",
            return_value=content,
        ) as content_builder, patch(
            "mark_api.protected_actor.CompletionAuthorityServer",
        ) as actor_server:
            runtime = create_protected_actor_runtime(
                socket_path=Path(cfg["socket"]),
                witness_socket=Path(cfg["witness_socket"]),
                owner_uid=cfg["owner_uid"], owner_gid=cfg["owner_gid"],
                witness_uid=cfg["witness_uid"], runtime_uid=cfg["runtime_uid"],
                principal=cfg["principal"], cdp_port=cfg["cdp_port"],
                media_enabled=False,
            )
            self.assertIsInstance(runtime, ProtectedActorRuntime)
            kwargs = actor_server.call_args.kwargs
            service = kwargs["service"]
            self.assertIsNone(service._store)
            self.assertIsNone(service._writes._store)
            self.assertTrue(service._writes._writes_enabled)
            self.assertIs(service._state_writer, content.state_writer)
            self.assertIs(service._content_writer, content.content_writer)
            self.assertIs(service._create_writer, content.create_writer)
            self.assertEqual(kwargs["principal"], cfg["principal"])
            self.assertIsNone(kwargs["media_service"])
            self.assertIsNone(kwargs["media_stager"])
            content_builder.assert_called_once()
            runtime.close()
            actor_server.return_value.close.assert_called_once()
            content.close.assert_called_once()


    @unittest.skipIf(os.geteuid() == 0, "actor must have nonroot Linux UID")
    def test_media_wiring_uses_independent_verifier_and_preserves_staged_bytes(self):
        from mark_api.private_web_runtime import (
            PrivateWebMediaCreateRuntime, PrivateWebMediaCreateService,
        )
        from mark_api.private_web_media import PrivateWebMediaHandleStore
        from mark_api.private_web_media_verify import PrivateWebPublicMediaPersistenceVerifier

        cfg = parameters()
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "actor-private-media"
            media_runtime = PrivateWebMediaCreateRuntime(page_factory=lambda: MagicMock())
            with patch("mark_api.protected_actor._attest_witness_socket"), patch(
                "mark_api.protected_actor.build_protected_actor_content_runtime",
                return_value=MagicMock(),
            ), patch(
                "mark_api.protected_actor.build_protected_actor_media_runtime",
                return_value=media_runtime,
            ), patch(
                "mark_api.protected_actor.CompletionAuthorityServer",
            ) as actor_server:
                runtime = create_protected_actor_runtime(
                    socket_path=Path(cfg["socket"]),
                    witness_socket=Path(cfg["witness_socket"]),
                    owner_uid=cfg["owner_uid"], owner_gid=cfg["owner_gid"],
                    witness_uid=cfg["witness_uid"], runtime_uid=cfg["runtime_uid"],
                    principal=cfg["principal"], cdp_port=cfg["cdp_port"],
                    media_enabled=True, media_directory=media_dir,
                )
                params = actor_server.call_args.kwargs
                self.assertIsInstance(params["media_service"], PrivateWebMediaCreateService)
                self.assertIsInstance(
                    params["media_service"]._media_persistence_verifier,
                    PrivateWebPublicMediaPersistenceVerifier,
                )
                self.assertIs(params["media_stager"], runtime.media_handles)
                self.assertIsNone(params["media_service"]._store)
                self.assertEqual(media_dir.stat().st_mode & 0o777, 0o700)
                from base64 import b64decode
                png = b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Zr4jV8AAAAASUVORK5CYII=")
                ref = runtime.media_handles.stage_media("synthetic.png", png)
                self.assertEqual(len(runtime.media_handles.resolve((ref,))), 1)
                runtime.close()
                # An owner-pending media reference must not be blindly
                # deleted simply because an actor received SIGTERM.
                reopened = PrivateWebMediaHandleStore(directory=media_dir)
                self.assertEqual(len(reopened.resolve((ref,))), 1)
                reopened.discard((ref,))
                reopened.close()


    def test_unclear_actor_shutdown_must_not_close_live_browser(self):
        authority = MagicMock()
        authority.close.side_effect = RuntimeError("synthetic active write")
        content = MagicMock()
        media = MagicMock()
        runtime = ProtectedActorRuntime(authority, content, media, MagicMock())
        with self.assertRaises(RuntimeError):
            runtime.close()
        content.close.assert_not_called()
        media.close.assert_not_called()

    def test_unsettled_media_close_must_not_clear_media_or_browser(self):
        authority = MagicMock()
        media = MagicMock()
        media.close.side_effect = RuntimeError("synthetic unsettled submit")
        content = MagicMock()
        handles = MagicMock()
        runtime = ProtectedActorRuntime(authority, content, media, handles)
        with self.assertRaises(RuntimeError):
            runtime.close()
        authority.close.assert_called_once()
        handles.close.assert_not_called()
        content.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()