"""Synthetic real-HTTP contract checks; no OpenAI or Kleinanzeigen access."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
from types import SimpleNamespace
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from mark_api.dashboard import (
    DashboardGenerationAccess, create_server, main as dashboard_main,
)
from mark_api.generation import AdvertisementDraft, GenerationError
from mark_api.storage import SnapshotStore


def _chunk(tag, payload):
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))


def valid_png():
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1024, 1024, 8, 6, 0, 0, 0)
    raster = (b"\x00" + b"\x00" * 4096) * 1024
    return (header + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raster)) + _chunk(b"IEND", b""))


class FakeGenerator:
    def __init__(self):
        self.text_calls = []
        self.image_calls = []
        self.error = None

    def generate_text(self, prompt):
        self.text_calls.append(prompt)
        if self.error:
            raise self.error
        return AdvertisementDraft(title="Fahrrad 28 Zoll", description="Ein eigener Artikel")

    def generate_image(self, prompt):
        self.image_calls.append(prompt)
        if self.error:
            raise self.error
        return valid_png()


@contextlib.contextmanager
def server_context(access, *, public_origin_port=None):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "mark.sqlite"
        server = create_server(
            SnapshotStore(db, create_if_missing=True), port=0,
            generation_access=access, public_origin_port=public_origin_port,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1], db
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            if thread.is_alive():
                raise AssertionError("test HTTP server failed to stop")


def http_call(port, method, path, body=b"", headers=None):
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        con.request(method, path, body=body, headers=headers or {})
        r = con.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        con.close()


def headers(port):
    return {
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/json",
        "X-Mark-Generation": "1",
        "X-Mark-Generation-Token": "s" * 32,
        "Sec-Fetch-Site": "same-origin",
    }


def body(mode="text", consent=True, prompt="Ein Fahrrad"):
    return json.dumps({
        "mode": mode, "prompt": prompt, "acknowledge_provider_cost": consent,
    }).encode()


class GenerationHttpTests(unittest.TestCase):
    def setUp(self):
        self.generator = FakeGenerator()
        self.access = DashboardGenerationAccess(
            ui_token="s" * 32,
            generator_factory=lambda: self.generator,
            max_requests=2,
        )

    def test_default_off_returns_404_and_no_platform_writer(self):
        with server_context(None) as (port, _):
            status, _, data = http_call(port, "GET", "/api/dashboard/config")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data), {
                "write_ui_available": False,
            })
            code, _, _ = http_call(port, "POST", "/api/dashboard/generate", body(), headers(port))
            self.assertEqual(code, 404)
            self.assertEqual(http_call(port, "POST", "/api/write/ads", b"{}")[0], 405)
        self.assertEqual(self.generator.text_calls, [])

    def test_separate_generation_token_origin_marker_and_host_required(self):
        with server_context(self.access) as (port, _):
            valid = headers(port)
            for change in (
                {"X-Mark-Generation": "0"},
                {"X-Mark-Generation-Token": "wrong"},
                {"Origin": "http://attacker.invalid"},
                {"Sec-Fetch-Site": "cross-site"},
                {"Authorization": "Bearer leaked"},
            ):
                with self.subTest(change=change):
                    self.assertEqual(http_call(
                        port, "POST", "/api/dashboard/generate", body(),
                        {**valid, **change},
                    )[0], 403)
            for absent in ("Origin", "X-Mark-Generation", "X-Mark-Generation-Token"):
                with self.subTest(absent=absent):
                    self.assertEqual(http_call(
                        port, "POST", "/api/dashboard/generate", body(),
                        {k: v for k, v in valid.items() if k != absent},
                    )[0], 403)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(),
                {**valid, "Host": "attacker.invalid"},
            )[0], 421)
        self.assertEqual(self.generator.text_calls, [])

    def test_protected_public_ingress_generation_origin_matches_validated_host(self):
        with server_context(self.access, public_origin_port=8875) as (port, _):
            public = {
                **headers(port),
                "Host": "127.0.0.1:8875",
                "Origin": "http://127.0.0.1:8875",
            }
            code, _, data = http_call(
                port, "POST", "/api/dashboard/generate", body(), public,
            )
            self.assertEqual(code, 200, data)
            self.assertIs(json.loads(data)["published"], False)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(),
                {**public, "Origin": "http://127.0.0.1:9999"},
            )[0], 403)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(),
                {**public, "Host": "127.0.0.1:" + str(port)},
            )[0], 403)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(),
                {**public, "Host": "attacker.invalid"},
            )[0], 421)
        self.assertEqual(self.generator.text_calls, ["Ein Fahrrad"])


    def test_non_ascii_token_or_origin_is_rejected_without_provider_call(self):
        # BaseHTTPRequestHandler decodes HTTP header bytes as latin-1.
        # hmac.compare_digest(str, str) raises TypeError for non-ASCII.
        with server_context(self.access) as (port, _):
            for changed in (
                {"X-Mark-Generation-Token": "s" * 31 + "\xff"},
                {"Origin": f"http://127.0.0.1:{port}/\xff"},
            ):
                with self.subTest(changed=changed):
                    code, _, response = http_call(
                        port, "POST", "/api/dashboard/generate",
                        body(), {**headers(port), **changed},
                    )
                    self.assertEqual(code, 403)
                    self.assertEqual(
                        json.loads(response), {"error": "generation_session_required"},
                    )
        self.assertEqual(self.generator.text_calls, [])
        self.assertEqual(self.generator.image_calls, [])

    def test_mode_specific_pricing_is_visible_before_single_use_consent(self):
        import inspect
        from mark_api.generation import AdvertisementGenerator
        with server_context(self.access) as (port, _):
            status, _, page = http_call(port, "GET", "/")
        self.assertEqual(status, 200)
        text_model = inspect.signature(
            AdvertisementGenerator.generate_text
        ).parameters["model"].default
        image_model = inspect.signature(
            AdvertisementGenerator.generate_image
        ).parameters["model"].default
        self.assertEqual(text_model, "gpt-5.6-terra")
        self.assertEqual(image_model, "gpt-image-2.5-flare")
        for value in (
            b'id="generation-pricing-note"',
            text_model.encode(), image_model.encode(),
            b"2.00 USD", b"12.00 USD", b"5.00 USD", b"30.00 USD",
            b"developers.openai.com/api/docs/pricing",
        ):
            with self.subTest(value=value):
                self.assertIn(value, page)
        self.assertLess(
            page.index(b'id="generation-pricing-note"'),
            page.index(b'id="generation-consent"'),
        )
        self.assertIn(
            b"Preisinformation oben", page,
        )

    def test_single_request_cost_consent_and_budget(self):
        with server_context(self.access) as (port, _):
            for invalid in (body(consent=False), body(prompt=" "), body(mode="delete"),
                            b'{"mode":"text","prompt":"a"}', b"{"):
                self.assertEqual(http_call(
                    port, "POST", "/api/dashboard/generate", invalid, headers(port),
                )[0], 400)
            self.assertEqual(self.generator.text_calls, [])
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(), headers(port),
            )[0], 200)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(), headers(port),
            )[0], 200)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(), headers(port),
            )[0], 429)
        self.assertEqual(self.generator.text_calls, ["Ein Fahrrad"] * 2)

    def test_real_multiline_and_tab_prompt_reaches_provider_once(self):
        prompt = "Ein eigenes Fahrrad\nMit echten Maßen\tund Rechnung"
        with server_context(self.access) as (port, _):
            code, _, response = http_call(
                port, "POST", "/api/dashboard/generate",
                body(prompt=prompt), headers(port),
            )
            self.assertEqual(code, 200, response)
            self.assertEqual(json.loads(response)["status"], "draft")
        self.assertEqual(self.generator.text_calls, [prompt])

    def test_text_image_output_is_draft_only_without_platform_mutation(self):
        with server_context(self.access) as (port, _):
            code, response_headers, page = http_call(port, "GET", "/")
            self.assertEqual(code, 200)
            self.assertIn(b"generation-panel", page)
            self.assertIn("img-src 'self' blob:", response_headers["Content-Security-Policy"])
            self.assertNotIn(b"generation_token=", page)
            code, _, config = http_call(port, "GET", "/api/dashboard/config")
            self.assertTrue(json.loads(config)["generation_ui_available"])
            self.assertFalse(json.loads(config)["write_ui_available"])
            code, _, data = http_call(
                port, "POST", "/api/dashboard/generate", body(), headers(port),
            )
            self.assertEqual(code, 200)
            payload = json.loads(data)
            self.assertEqual(payload["title"], "Fahrrad 28 Zoll")
            self.assertEqual(payload["status"], "draft")
            self.assertIs(payload["published"], False)
            code, h, image = http_call(
                port, "POST", "/api/dashboard/generate",
                body(mode="image", prompt="Bild"), headers(port),
            )
            self.assertEqual(code, 200)
            self.assertEqual(h["Content-Type"], "image/png")
            self.assertEqual(image, valid_png())
            self.assertEqual(h["Cache-Control"], "no-store")
            self.assertEqual(http_call(
                port, "POST", "/api/write/ads", b"{}", headers(port),
            )[0], 405)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate?q=extra",
                body(), headers(port),
            )[0], 404)
        self.assertEqual(self.generator.text_calls, ["Ein Fahrrad"])
        self.assertEqual(self.generator.image_calls, ["Bild"])

    def test_upstream_failure_never_retries_or_discloses_secrets(self):
        self.generator.error = RuntimeError("provider key and sensitive prompt")
        access = DashboardGenerationAccess(
            ui_token="s" * 32, generator_factory=lambda: self.generator,
            max_requests=1,
        )
        with server_context(access) as (port, _):
            code, _, data = http_call(
                port, "POST", "/api/dashboard/generate", body(), headers(port),
            )
            self.assertEqual(code, 502)
            self.assertEqual(
                json.loads(data)["error"], "generation_failed_no_automatic_retry",
            )
            self.assertNotIn(b"provider", data)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(), headers(port),
            )[0], 429)
        self.assertEqual(len(self.generator.text_calls), 1)

    def test_deep_json_is_rejected_before_provider_and_does_not_spend_budget(self):
        # HTTP route must fail closed on json.loads RecursionError, not close the
        # connection with an uncaught handler exception after session auth.
        deeply_nested_json = b"[" * 1500 + b"0" + b"]" * 1500
        with server_context(self.access) as (port, _):
            code, _, data = http_call(
                port, "POST", "/api/dashboard/generate",
                deeply_nested_json, headers(port),
            )
            self.assertEqual(code, 400)
            self.assertEqual(json.loads(data), {"error": "invalid_generation_request"})
            self.assertEqual(self.generator.text_calls, [])
            code, _, data = http_call(
                port, "POST", "/api/dashboard/generate", body(), headers(port),
            )
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(data)["status"], "draft")
        self.assertEqual(self.generator.text_calls, ["Ein Fahrrad"])

    def test_content_size_type_and_idempotency_key_rejected_without_provider(self):
        with server_context(self.access) as (port, _):
            h = headers(port)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(),
                {**h, "Content-Type": "text/plain"},
            )[0], 400)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", b"x" * 16385, h,
            )[0], 400)
            self.assertEqual(http_call(
                port, "POST", "/api/dashboard/generate", body(),
                {**h, "Idempotency-Key": "forbidden"},
            )[0], 400)
        self.assertFalse(self.generator.text_calls)

    def test_inflight_duplicate_is_rejected_without_second_generation(self):
        entered, release = threading.Event(), threading.Event()
        class BlockGenerator:
            def generate_text(self, _prompt):
                entered.set()
                if not release.wait(5):
                    raise GenerationError("simulated timeout")
                return AdvertisementDraft(title="Erster", description="Erste Anfrage")
        access = DashboardGenerationAccess(
            ui_token="s" * 32, generator_factory=BlockGenerator, max_requests=2,
        )
        with server_context(access) as (port, _):
            results = []
            first = threading.Thread(target=lambda: results.append(
                http_call(port, "POST", "/api/dashboard/generate",
                          body(), headers(port))[0]
            ))
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(http_call(
                    port, "POST", "/api/dashboard/generate",
                    body(), headers(port),
                )[0], 409)
            finally:
                release.set()
                first.join(timeout=4)
            self.assertEqual(results, [200])

    def test_cli_requires_opt_in_and_existing_credentials_before_db_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite"
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        dashboard_main([
                            "--db", str(missing), "--enable-generation",
                        ])
            self.assertFalse(missing.exists())
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-only"}),
                patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=object)}),
            ):
                with server_context(None) as (port, db):
                    class DummyServer:
                        server_address = ("127.0.0.1", port)
                        def serve_forever(self):
                            return None
                        def server_close(self):
                            return None
                    with (
                        patch("mark_api.dashboard.create_server", return_value=DummyServer()),
                        contextlib.redirect_stdout(io.StringIO()) as captured,
                    ):
                        self.assertEqual(dashboard_main([
                            "--db", str(db), "--port", "0",
                            "--enable-generation",
                        ]), 0)
                    self.assertIn("#generation_token=", captured.getvalue())
                    self.assertNotIn("synthetic-only", captured.getvalue())

    def test_missing_openai_extra_fails_before_db_bootstrap_and_http_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "new.sqlite"
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-only"}),
                patch.dict(sys.modules, {"openai": None}),
                patch("mark_api.dashboard.create_server") as builder,
                contextlib.redirect_stderr(io.StringIO()) as error,
            ):
                with self.assertRaises(SystemExit) as caught:
                    dashboard_main([
                        "--db", str(missing), "--init-db", "--enable-generation",
                    ])
            self.assertEqual(caught.exception.code, 2)
            self.assertFalse(missing.exists())
            builder.assert_not_called()
            self.assertIn("mark-api[generation]", error.getvalue())
            self.assertNotIn("synthetic-only", error.getvalue())

    def test_access_rejects_unsafe_token_and_budget(self):
        for token in ("short", "bad\nstring", "a" * 513, "ä" * 32):
            with self.assertRaises(ValueError):
                DashboardGenerationAccess(ui_token=token)
        for count in (0, -1, True, 101):
            with self.assertRaises(ValueError):
                DashboardGenerationAccess(ui_token="s" * 32, max_requests=count)


class GenerationBrowserStateTests(unittest.TestCase):
    def test_retry_invalidates_stale_text_and_image_drafts_in_real_javascript(self):
        """Run the shipped browser handlers without network or platform calls."""
        import shutil
        import subprocess
        from mark_api.dashboard import _DASHBOARD_JS

        if shutil.which("node") is None:
            self.skipTest("Node.js required for real browser-JavaScript state test")
        start = _DASHBOARD_JS.index("function generationUiReady() {")
        end = _DASHBOARD_JS.index('if (typeof window !== "undefined" && typeof window.addEventListener === "function")')
        handlers = _DASHBOARD_JS[start:end]
        script = r"""
const assert = require("node:assert/strict");
const source = JSON.parse(process.argv[1]);
const ids = [
  "generation-panel", "generation-session-note", "generation-text",
  "generation-image", "generation-consent", "generation-apply-text",
  "generation-apply-image", "generation-status", "generation-prompt",
  "generation-draft-title", "generation-draft-description",
  "generation-text-preview", "generation-draft-image",
  "generation-image-preview", "generation-text-prompt", "generation-image-prompt",
  "create-title", "create-description",
];
const dom = Object.create(null);
for (const id of ids) {
  dom[id] = {
    hidden: true, disabled: false, checked: false, value: "",
    textContent: "", src: "",
    removeAttribute(key) { if (key === "src") this.src = ""; },
  };
}
globalThis.byId = (id) => {
  if (!dom[id]) throw Error("unexpected browser element " + id);
  return dom[id];
};
globalThis.generationUiAvailable = true;
globalThis.generationToken = "s".repeat(32);
globalThis.generationBusy = false;
globalThis.generationTextDraft = null;
globalThis.generationImageBlob = null;
globalThis.generationImageSourcePrompt = null;
globalThis.generationImagePreviewUrl = null;
globalThis.createInFlight = false;
globalThis.writeUiReady = () => true;
const revoked = [];
globalThis.URL = {
  createObjectURL: () => "blob:test-image-1",
  revokeObjectURL: (value) => revoked.push(value),
};
globalThis.window = {confirm: () => false};
const {requestGeneration, onGenerationPromptChange, applyGenerationTextToCreate, applyGenerationImageToCreate} =
  new Function(source + "\nreturn {requestGeneration, onGenerationPromptChange, applyGenerationTextToCreate, applyGenerationImageToCreate};")();
let response;
let networkCalls = 0;
globalThis.fetch = async (url, options) => {
  networkCalls++;
  assert.equal(url, "/api/dashboard/generate");
  assert.equal(options.method, "POST");
  assert.equal(options.credentials, "omit");
  return response;
};
const textOk = {
  ok: true, status: 200,
  json: async () => ({
    status: "draft", published: false, title: "Früherer Titel",
    description: "Frühere Beschreibung",
  }),
};
const imageOk = {
  ok: true, status: 200, headers: {get: () => "image/png"},
  blob: async () => new Blob(["image"], {type: "image/png"}),
};
(async () => {
  dom["generation-prompt"].value = "Alter Prompt";
  dom["generation-consent"].checked = true;
  response = textOk;
  await requestGeneration("text");
  assert.equal(generationTextDraft.title, "Früherer Titel");
  assert.equal(dom["generation-apply-text"].disabled, false);
  assert.equal(dom["generation-text-preview"].hidden, false);

  dom["generation-prompt"].value = "Neuer Prompt";
  dom["generation-consent"].checked = true;
  response = {ok: false, status: 502, json: async () => ({error: "provider_failed"})};
  await requestGeneration("text");
  assert.equal(generationTextDraft, null);
  assert.equal(dom["generation-draft-title"].value, "");
  assert.equal(dom["generation-draft-description"].value, "");
  assert.equal(dom["generation-text-preview"].hidden, true);
  assert.equal(dom["generation-apply-text"].disabled, true);
  assert.match(dom["generation-status"].textContent, /fehlgeschlagen/);

  dom["generation-prompt"].value = "Bildidee";
  dom["generation-consent"].checked = true;
  response = imageOk;
  await requestGeneration("image");
  assert.equal(generationImageBlob.size, 5);
  assert.equal(dom["generation-apply-image"].disabled, false);
  assert.equal(dom["generation-image-preview"].hidden, false);
  assert.equal(dom["generation-draft-image"].src, "blob:test-image-1");

  const before = networkCalls;
  dom["generation-prompt"].value = "";
  dom["generation-consent"].checked = true;
  await requestGeneration("image");
  assert.equal(networkCalls, before);
  assert.equal(generationImageBlob, null);
  assert.equal(generationImagePreviewUrl, null);
  assert.deepEqual(revoked, ["blob:test-image-1"]);
  assert.equal(dom["generation-draft-image"].src, "");
  assert.equal(dom["generation-image-preview"].hidden, true);
  assert.equal(dom["generation-apply-image"].disabled, true);
  assert.match(dom["generation-status"].textContent, /eingeben/);
  // Keep the provider response pending while the user-visible prompt is
  // changed by another DOM actor. Neither obsolete result may be applied.
  let releaseProvider;
  globalThis.fetch = (url, options) => {
    networkCalls++;
    assert.equal(url, "/api/dashboard/generate");
    assert.equal(options.method, "POST");
    return new Promise((resolve) => { releaseProvider = resolve; });
  };
  dom["generation-prompt"].value = "Text vor Anfrage";
  dom["generation-consent"].checked = true;
  const pendingText = requestGeneration("text");
  assert.equal(dom["generation-prompt"].disabled, true);
  assert.equal(dom["generation-apply-text"].disabled, true);
  dom["generation-prompt"].value = "Text nach Anfrage";
  releaseProvider(textOk);
  await pendingText;
  assert.equal(generationTextDraft, null);
  assert.equal(dom["generation-draft-title"].value, "");
  assert.equal(dom["generation-text-preview"].hidden, true);
  assert.equal(dom["generation-apply-text"].disabled, true);
  assert.equal(dom["generation-prompt"].disabled, false);
  assert.match(dom["generation-status"].textContent, /verworfen/);

  dom["generation-prompt"].value = "Bild vor Anfrage";
  dom["generation-consent"].checked = true;
  const pendingImage = requestGeneration("image");
  assert.equal(dom["generation-prompt"].disabled, true);
  dom["generation-prompt"].value = "Bild nach Anfrage";
  releaseProvider(imageOk);
  await pendingImage;
  assert.equal(generationImageBlob, null);
  assert.equal(generationImagePreviewUrl, null);
  assert.equal(dom["generation-image-preview"].hidden, true);
  assert.equal(dom["generation-apply-image"].disabled, true);
  assert.equal(dom["generation-prompt"].disabled, false);
  assert.deepEqual(revoked, ["blob:test-image-1"]);
  assert.match(dom["generation-status"].textContent, /verworfen/);

  // A programmatic prompt change without an input event may not permit a
  // stale draft to be applied, even when the DOM button is still enabled.
  const textPrompt = "Artikel A";
  const imagePrompt = "Illustration B";
  dom["generation-prompt"].value = textPrompt;
  dom["generation-consent"].checked = true;
  globalThis.fetch = async () => textOk;
  await requestGeneration("text");
  assert.equal(generationTextDraft.sourcePrompt, textPrompt);
  assert.match(dom["generation-text-prompt"].textContent, /Artikel A/);
  dom["generation-prompt"].value = imagePrompt;
  applyGenerationTextToCreate();
  assert.equal(dom["create-title"].value, "");
  assert.match(dom["generation-status"].textContent, /anderem Prompt/);

  // A new request for a different prompt invalidates the opposite-mode
  // draft, not merely the mode selected by the user.
  dom["generation-consent"].checked = true;
  globalThis.fetch = async () => imageOk;
  await requestGeneration("image");
  assert.equal(generationTextDraft, null);
  assert.equal(dom["generation-text-preview"].hidden, true);
  assert.equal(dom["generation-text-prompt"].textContent, "");
  assert.equal(dom["generation-apply-text"].disabled, true);
  assert.equal(generationImageSourcePrompt, imagePrompt);
  assert.match(dom["generation-image-prompt"].textContent, /Illustration B/);

  dom["create-media"] = {files: []};
  dom["generation-prompt"].value = "Prompt C";
  applyGenerationImageToCreate();
  assert.equal(dom["create-media"].files.length, 0);
  assert.match(dom["generation-status"].textContent, /anderem Prompt/);

  // Matching prompt allows explicit local image attachment without uploading.
  dom["generation-prompt"].value = imagePrompt;
  globalThis.File = class {
    constructor(parts, name, options) { this.name = name; this.type = options.type; this.size = parts[0].size; }
  };
  globalThis.DataTransfer = class {
    constructor() {
      this.files = [];
      this.items = {add: (value) => this.files.push(value)};
    }
  };
  applyGenerationImageToCreate();
  assert.equal(dom["create-media"].files.length, 1);
  assert.equal(dom["create-media"].files[0].name, "mark-ki-entwurf.png");

  // Real user input invalidates both caches, clears source labels and revokes
  // the previous blob URL (even if a previous generation succeeded).
  dom["generation-prompt"].value = "Artikel D";
  onGenerationPromptChange();
  assert.equal(generationTextDraft, null);
  assert.equal(generationImageBlob, null);
  assert.equal(generationImageSourcePrompt, null);
  assert.equal(generationImagePreviewUrl, null);
  assert.equal(dom["generation-image-prompt"].textContent, "");
  assert.equal(dom["generation-image-preview"].hidden, true);
  assert.equal(dom["generation-apply-image"].disabled, true);
  assert.deepEqual(revoked, ["blob:test-image-1", "blob:test-image-1"]);

  dom["generation-consent"].checked = true;
  globalThis.fetch = async () => textOk;
  await requestGeneration("text");
  assert.equal(generationTextDraft.sourcePrompt, "Artikel D");
  applyGenerationTextToCreate();
  assert.equal(dom["create-title"].value, "Früherer Titel");
  assert.equal(dom["create-description"].value, "Frühere Beschreibung");
  console.log("JS_DRAFT_RESET_PASS");
})().catch((err) => { console.error(err); process.exitCode = 1; });
"""
        completed = subprocess.run(
            ["node", "-e", script, json.dumps(handlers)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(
            completed.returncode, 0, completed.stdout + completed.stderr,
        )
        self.assertIn("JS_DRAFT_RESET_PASS", completed.stdout)


if __name__ == "__main__":
    unittest.main()