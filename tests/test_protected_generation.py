"""No-provider, real UDS transport tests for the optional protected AI worker."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
from threading import Thread
import unittest
import zlib
from unittest.mock import patch

from mark_api.generation import AdvertisementDraft, GenerationError
from mark_api.protected_generation import (
    ProtectedGenerationClient, ProtectedGenerationServer,
    _private_config,
)


def _chunk(name, payload):
    return (struct.pack(">I", len(payload)) + name + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xffffffff))


def _png():
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1024, 1024, 8, 6, 0, 0, 0)
    raster = (b"\x00" + b"\x00" * 4096) * 1024
    return (header + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raster)) + _chunk(b"IEND", b""))


class _FakeGenerator:
    def __init__(self):
        self.prompts = []
        self.fail = False

    def generate_text(self, prompt):
        self.prompts.append(("text", prompt))
        if self.fail:
            raise RuntimeError("synthetic sensitive provider detail")
        return AdvertisementDraft(title="Lokaler Testtitel",
                                  description="Lokale Testbeschreibung")

    def generate_image(self, prompt):
        self.prompts.append(("image", prompt))
        if self.fail:
            raise RuntimeError("synthetic sensitive provider detail")
        return _png()


@contextmanager
def _serve(generator):
    with tempfile.TemporaryDirectory(prefix="protected-generation-") as tmp:
        parent = Path(tmp) / "service"
        parent.mkdir()
        os.chmod(parent, 0o2710)
        if os.geteuid() != os.getegid():
            os.chown(parent, os.geteuid(), os.geteuid())
        path = parent / "gen.sock"
        server = ProtectedGenerationServer(
            socket_path=path, frontend_uid=os.geteuid(),
            generator_factory=lambda: generator,
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield path
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=8)
            if thread.is_alive():
                raise AssertionError("generation server thread did not drain")


class ProtectedGenerationTransportTests(unittest.TestCase):
    def test_real_peer_authenticated_unix_text_and_image_drafts_no_publish(self):
        fake = _FakeGenerator()
        with _serve(fake) as path:
            client = ProtectedGenerationClient(socket_path=path, worker_uid=os.geteuid())
            text = client.generate_text("Ein Schrank")
            self.assertEqual(text.as_dict()["published"], False)
            self.assertEqual(text.title, "Lokaler Testtitel")
            image = client.generate_image("Ein altes Möbel")
            self.assertEqual(image, _png())
            self.assertEqual(fake.prompts, [
                ("text", "Ein Schrank"), ("image", "Ein altes Möbel"),
            ])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o660)
        self.assertFalse(path.exists())

    def test_unexpected_or_untrusted_peer_never_reaches_provider(self):
        fake = _FakeGenerator()
        with _serve(fake) as path:
            client = ProtectedGenerationClient(
                socket_path=path, worker_uid=os.geteuid() + 1,
            )
            with self.assertRaises(GenerationError):
                client.generate_text("Privater Entwurf")
            self.assertEqual(fake.prompts, [])

    def test_bounded_manual_cost_budget_and_error_never_retries(self):
        fake = _FakeGenerator()
        with _serve(fake) as path:
            client = ProtectedGenerationClient(socket_path=path, worker_uid=os.geteuid())
            fake.fail = True
            with self.assertRaises(GenerationError):
                client.generate_text("Testfehler")
            self.assertEqual(fake.prompts, [("text", "Testfehler")])
            fake.fail = False
            for _ in range(9):
                self.assertEqual(client.generate_text("Test").title, "Lokaler Testtitel")
            with self.assertRaises(GenerationError):
                client.generate_image("Budget verweigern")
        self.assertEqual(len(fake.prompts), 10)

    def test_nonconsenting_bad_frames_have_no_provider_effect(self):
        fake = _FakeGenerator()
        with _serve(fake) as path:
            for request in (
                {"version": 1, "mode": "text", "prompt": "Test",
                 "acknowledge_provider_cost": False},
                {"version": 1, "mode": "text", "prompt": "Test",
                 "acknowledge_provider_cost": True, "extra": "bad"},
                {"version": 1, "mode": "delete", "prompt": "Test",
                 "acknowledge_provider_cost": True},
            ):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(5)
                    peer.connect(str(path))
                    peer.sendall(json.dumps(request).encode() + b"\n")
                    reply = peer.recv(2048)
                    self.assertIn(b'"ok":false', reply)
            self.assertEqual(fake.prompts, [])
            client = ProtectedGenerationClient(socket_path=path, worker_uid=os.geteuid())
            self.assertEqual(client.generate_text("One valid").title, "Lokaler Testtitel")
        self.assertEqual(fake.prompts, [("text", "One valid")])

    def test_missing_or_wrong_owned_generation_directory_fails_before_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "dir"
            parent.mkdir()
            location = parent / "gen.sock"
            with self.assertRaises(ValueError):
                ProtectedGenerationServer(
                    socket_path=location, frontend_uid=os.geteuid(),
                )
            self.assertFalse(location.exists())

    def test_private_config_requires_pipe_exact_schema_and_key_never_echoes(self):
        secret = "only-synthetic-generation-credential-do-not-use"
        good = {"socket": "/tmp/example-protected-gen.sock",
                "frontend_uid": 50042, "generation_key": secret}
        for variant in (
            good,
            {**good, "unexpected": True},
            {**good, "generation_key": ""},
            {**good, "socket": "/tmp/../tmp/example.sock"},
        ):
            rd, wr = os.pipe()
            try:
                os.write(wr, json.dumps(variant).encode() + b"\n")
            finally:
                os.close(wr)
            try:
                if variant == good:
                    self.assertEqual(_private_config(rd), good)
                else:
                    with self.assertRaises(ValueError):
                        _private_config(rd)
            finally:
                os.close(rd)


if __name__ == "__main__":
    unittest.main()
