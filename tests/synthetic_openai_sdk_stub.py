"""Strictly test-only, non-network OpenAI stand-in for isolated wheel acceptance.

This file is NEVER packaged in Kleinanzeiger. The CI job installs it as
openai/__init__.py into a disposable isolated wheel virtualenv. It makes
no HTTP, network or provider calls and cannot spend money.
"""
from __future__ import annotations

import base64
import errno
import socket
import json
import struct
import zlib
from types import SimpleNamespace


def _chunk(name: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + name + data
            + struct.pack(">I", zlib.crc32(name + data) & 0xffffffff))


def _png() -> bytes:
    # Complete, valid 1024x1024 non-interlaced RGBA raster, compressed inline.
    ihdr = struct.pack(">IIBBBBB", 1024, 1024, 8, 6, 0, 0, 0)
    image = (b"\x00" + b"\x00" * 4096) * 1024
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(image)) + _chunk(b"IEND", b""))


def _assert_cdp_blocked() -> None:
    # The isolated CI PID/net namespace owns a synthetic CDP listener on this
    # port. A hostile provider library must NOT be able to connect to it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", 19322))
        except OSError as exc:
            if exc.errno != errno.EACCES:
                raise RuntimeError("AI egress isolation not attested") from None
        else:
            raise RuntimeError("AI worker reached protected browser CDP")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", 19323))
        except OSError as exc:
            if exc.errno != errno.EACCES:
                raise RuntimeError("AI worker TCP bind not denied") from None
        else:
            raise RuntimeError("AI worker could bind a host TCP listener")


class _Responses:
    def create(self, *, model, instructions, input, text):
        if (model != "gpt-5.6-terra" or not input or not instructions
                or text["format"]["type"] != "json_schema"):
            raise ValueError("unexpected synthetic text model contract")
        _assert_cdp_blocked()
        return SimpleNamespace(output_text=json.dumps({
            "title": "Synthetic generated title",
            "description": "Synthetic generated description, no publication.",
        }))


class _Images:
    def generate(self, *, model, prompt, n, size, output_format):
        if (model != "gpt-image-2.5-flare" or not prompt
                or n != 1 or size != "1024x1024" or output_format != "png"):
            raise ValueError("unexpected synthetic image model contract")
        _assert_cdp_blocked()
        return SimpleNamespace(data=[
            SimpleNamespace(b64_json=base64.b64encode(_png()).decode("ascii"))
        ])


class OpenAI:
    """No sockets, SDK client or real service connection exists in this stub."""

    def __init__(self, *, max_retries=0, timeout=120.0):
        if max_retries != 0 or timeout != 120.0:
            raise ValueError("nonretrying local generation only")
        self.responses = _Responses()
        self.images = _Images()