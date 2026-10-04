from __future__ import annotations

import importlib.util
import io
import os
import time
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path

from mark_api.private_web_media import PrivateWebMediaSource
from mark_api.private_web_media_verify import (
    PrivateWebPublicMediaPersistenceVerifier,
    _Fetched,
    _ImageSignature,
    _NoRedirectHandler,
    _has_exact_matching,
    _parse_listing_gallery,
    _read_local_source,
    _signatures_match,
)
from mark_api.results import ReadStatus


AD_ID = "1234567890"
NOW = datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc)
IMAGE_A = (
    "https://img.kleinanzeigen.de/api/v1/prod-ads/images/"
    "aa/aa111111-1111-4111-8111-111111111111?rule=$_59.JPG"
)
IMAGE_B = (
    "https://img.kleinanzeigen.de/api/v1/prod-ads/images/"
    "bb/bb222222-2222-4222-8222-222222222222?rule=$_59.AUTO"
)
IMAGE_C = (
    "https://img.kleinanzeigen.de/api/v1/prod-ads/images/"
    "cc/cc333333-3333-4333-8333-333333333333?rule=$_2.AUTO"
)


def listing_html(
    *urls: str,
    canonical_ad_id: str = AD_ID,
    second_gallery: bool = False,
) -> bytes:
    images = "".join(
        (
            '<div class="galleryimage-element" data-ix="%d">'
            '<script type="application/ld+json">'
            '{"@type":"ImageObject","contentUrl":"%s"}'
            "</script></div>"
        )
        % (index, url)
        for index, url in enumerate(urls)
    )
    extra = (
        '<div class="vip-image-gallery j-gallery-image">'
        '<div class="galleryimage-element">'
        '<script type="application/ld+json">'
        '{"@type":"ImageObject","contentUrl":"%s"}'
        "</script></div></div>"
        % IMAGE_C
        if second_gallery
        else ""
    )
    recommendation = (
        '<section class="recommendation">'
        '<script type="application/ld+json">'
        '{"@type":"ImageObject","contentUrl":"%s"}'
        "</script></section>"
        % IMAGE_C
    )
    return (
        "<html><head>"
        '<link rel="canonical" href="https://www.kleinanzeigen.de/'
        f's-anzeige/test/{canonical_ad_id}-1-2">'
        "</head><body>"
        '<div class="vip-image-gallery galleryimage-large j-gallery-image">'
        f"{images}</div>{extra}{recommendation}"
        "</body></html>"
    ).encode("utf-8")


def signature(
    value: int,
    *,
    width: int = 1200,
    height: int = 800,
) -> _ImageSignature:
    return _ImageSignature(
        width=width,
        height=height,
        normalized_rgb=bytes([value]) * 96,
    )


class _FakeMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError("sleep must not be negative")
        self.value += seconds


class PrivateWebMediaVerifierParserTests(unittest.TestCase):
    def test_parser_reads_only_target_listing_gallery(self) -> None:
        actual = _parse_listing_gallery(
            listing_html(IMAGE_A, IMAGE_B),
            AD_ID,
        )

        self.assertEqual(
            actual,
            (
                IMAGE_A.replace("$_59.JPG", "$_57.JPG"),
                IMAGE_B.replace("$_59.AUTO", "$_57.JPG"),
            ),
        )
        self.assertNotIn(
            IMAGE_C.replace("$_2.AUTO", "$_57.JPG"),
            actual,
        )

    def test_parser_rejects_wrong_canonical_target(self) -> None:
        with self.assertRaisesRegex(Exception, "canonical ID mismatch"):
            _parse_listing_gallery(
                listing_html(IMAGE_A, canonical_ad_id="9999999999"),
                AD_ID,
            )

    def test_parser_rejects_multiple_galleries(self) -> None:
        with self.assertRaisesRegex(Exception, "gallery is not unique"):
            _parse_listing_gallery(
                listing_html(IMAGE_A, second_gallery=True),
                AD_ID,
            )

    def test_parser_rejects_untrusted_or_duplicate_gallery_urls(self) -> None:
        with self.assertRaisesRegex(Exception, "untrusted gallery image URL"):
            _parse_listing_gallery(
                listing_html(
                    "https://attacker.invalid/api/v1/prod-ads/images/"
                    "aa/aa111111-1111-4111-8111-111111111111?rule=$_59.JPG"
                ),
                AD_ID,
            )

        with self.assertRaisesRegex(Exception, "duplicate image IDs"):
            _parse_listing_gallery(
                listing_html(
                    IMAGE_A,
                    IMAGE_A.replace("$_59.JPG", "$_2.AUTO"),
                ),
                AD_ID,
            )

    def test_redirect_handler_never_follows_redirects(self) -> None:
        handler = _NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(
                object(),
                object(),
                302,
                "redirect",
                {},
                "https://attacker.invalid/",
            )
        )


@unittest.skipUnless(
    importlib.util.find_spec("PIL") is not None,
    "Pillow optional dependency is not installed",
)
class PrivateWebMediaVerifierPillowTests(unittest.TestCase):
    def test_signature_dimensions_follow_exif_transpose(self) -> None:
        from PIL import Image
        from mark_api.private_web_media_verify import _pillow_signature

        image = Image.new("RGB", (20, 30), (10, 20, 30))
        exif = Image.Exif()
        exif[274] = 6
        encoded = io.BytesIO()
        image.save(encoded, "JPEG", quality=95, exif=exif)

        result = _pillow_signature(encoded.getvalue())

        self.assertEqual((result.width, result.height), (30, 20))

    def test_real_jpeg_downscale_and_recompression_still_matches(self) -> None:
        from PIL import Image

        source = Image.new("RGB", (640, 480))
        pixels = source.load()
        for y in range(source.height):
            for x in range(source.width):
                pixels[x, y] = (
                    (x * 3 + y) % 256,
                    (x + y * 2) % 256,
                    (x * 2 + y * 3) % 256,
                )

        first = io.BytesIO()
        source.save(first, "JPEG", quality=95, subsampling=0)
        downscaled = source.resize(
            (400, 300),
            Image.Resampling.LANCZOS,
        )
        second = io.BytesIO()
        downscaled.save(second, "JPEG", quality=70, optimize=True)

        from mark_api.private_web_media_verify import _pillow_signature

        self.assertTrue(
            _signatures_match(
                _pillow_signature(first.getvalue()),
                _pillow_signature(second.getvalue()),
            )
        )

        unrelated = Image.new("RGB", (400, 300), (250, 10, 10))
        third = io.BytesIO()
        unrelated.save(third, "JPEG", quality=70)
        self.assertFalse(
            _signatures_match(
                _pillow_signature(first.getvalue()),
                _pillow_signature(third.getvalue()),
            )
        )

        localized = downscaled.copy()
        localized_pixels = localized.load()
        for y in range(20):
            for x in range(20):
                localized_pixels[x, y] = (255, 0, 0)
        fourth = io.BytesIO()
        localized.save(fourth, "JPEG", quality=70, optimize=True)
        self.assertFalse(
            _signatures_match(
                _pillow_signature(first.getvalue()),
                _pillow_signature(fourth.getvalue()),
            )
        )


class PrivateWebMediaVerifierIdentityTests(unittest.TestCase):
    def test_signature_matching_is_conservative(self) -> None:
        expected = signature(100)
        close = signature(102)
        far_pixels = signature(160)
        wrong_ratio = signature(
            106,
            width=1200,
            height=500,
        )
        localized = _ImageSignature(
            width=1200,
            height=800,
            normalized_rgb=bytes([100]) * 95 + bytes([250]),
        )

        self.assertTrue(_signatures_match(expected, close))
        self.assertFalse(_signatures_match(expected, far_pixels))
        self.assertFalse(_signatures_match(expected, wrong_ratio))
        self.assertFalse(_signatures_match(expected, localized))

    def test_exact_matching_requires_same_count_content_and_order(self) -> None:
        first = signature(30)
        second = signature(200)

        self.assertTrue(
            _has_exact_matching(
                (first, second),
                (first, second),
            )
        )
        self.assertFalse(
            _has_exact_matching(
                (first, second),
                (second, first),
            )
        )
        self.assertFalse(
            _has_exact_matching(
                (first, second),
                (first,),
            )
        )
        self.assertFalse(
            _has_exact_matching(
                (first, second),
                (first, first),
            )
        )


class PrivateWebPublicMediaPersistenceVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.first = Path(self.tmp.name) / "first.jpg"
        self.second = Path(self.tmp.name) / "second.jpg"
        self.first.write_bytes(b"local-first")
        self.second.write_bytes(b"local-second")
        self.sources = (
            PrivateWebMediaSource(str(self.first)),
            PrivateWebMediaSource(str(self.second)),
        )
        self.first_signature = signature(40)
        self.second_signature = signature(190)
        self.remote_first_signature = signature(43)
        self.remote_second_signature = signature(187)

    def test_local_source_read_works_without_o_cloexec(self) -> None:
        class _OsWithoutCloexec:
            def __getattr__(self, name: str):
                if name == "O_CLOEXEC":
                    raise AttributeError(name)
                return getattr(os, name)

        with patch(
            "mark_api.private_web_media_verify.os",
            _OsWithoutCloexec(),
        ):
            self.assertEqual(
                _read_local_source(self.sources[0]),
                b"local-first",
            )

    def _signature_loader(self, payload: bytes) -> _ImageSignature:
        mapping = {
            b"local-first": self.first_signature,
            b"local-second": self.second_signature,
            b"remote-first": self.remote_first_signature,
            b"remote-second": self.remote_second_signature,
            b"remote-wrong": signature(245),
        }
        return mapping[payload]

    def _exact_fetch(self, url: str, timeout: float, maximum: int) -> _Fetched:
        self.assertGreater(timeout, 0)
        self.assertGreater(maximum, 0)
        first_url = IMAGE_A.replace("$_59.JPG", "$_57.JPG")
        second_url = IMAGE_B.replace("$_59.AUTO", "$_57.JPG")
        if url == f"https://www.kleinanzeigen.de/s-anzeige/{AD_ID}":
            return _Fetched(
                final_url=url,
                content_type="text/html",
                body=listing_html(IMAGE_A, IMAGE_B),
            )
        if url == first_url:
            return _Fetched(url, "image/jpeg", b"remote-first")
        if url == second_url:
            return _Fetched(url, "image/jpeg", b"remote-second")
        raise AssertionError(f"unexpected URL {url}")

    def test_exact_gallery_match_confirms_persistence(self) -> None:
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=self._exact_fetch,
            signature_loader=self._signature_loader,
            clock=lambda: NOW,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )

        result = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=10.0,
        )

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        self.assertIsNotNone(result.value)
        assert result.value is not None
        self.assertEqual(result.value.ad_id, AD_ID)
        self.assertTrue(result.value.exact_match)
        self.assertEqual(
            result.value.source,
            "private-web-public-detail-gallery",
        )
        self.assertEqual(result.value.observed_at, NOW)

    def test_delayed_convergence_after_four_mismatches_confirms(self) -> None:
        first_url = IMAGE_A.replace("$_59.JPG", "$_57.JPG")
        second_url = IMAGE_B.replace("$_59.AUTO", "$_57.JPG")
        listing_calls = 0

        def fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            nonlocal listing_calls
            if url == f"https://www.kleinanzeigen.de/s-anzeige/{AD_ID}":
                listing_calls += 1
                visible = (IMAGE_A,) if listing_calls <= 4 else (IMAGE_A, IMAGE_B)
                return _Fetched(url, "text/html", listing_html(*visible))
            if url == first_url:
                return _Fetched(url, "image/jpeg", b"remote-first")
            if url == second_url:
                return _Fetched(url, "image/jpeg", b"remote-second")
            raise AssertionError(url)

        timer = _FakeMonotonic()
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=fetch,
            signature_loader=self._signature_loader,
            clock=lambda: NOW,
            monotonic=timer,
            sleep=timer.sleep,
        )

        result = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=2.0,
        )

        self.assertEqual(listing_calls, 5)
        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        assert result.value is not None
        self.assertTrue(result.value.exact_match)

    def test_complete_content_mismatch_returns_explicit_mismatch(self) -> None:
        first_url = IMAGE_A.replace("$_59.JPG", "$_57.JPG")
        second_url = IMAGE_B.replace("$_59.AUTO", "$_57.JPG")

        def fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            if url == f"https://www.kleinanzeigen.de/s-anzeige/{AD_ID}":
                return _Fetched(
                    url,
                    "text/html",
                    listing_html(IMAGE_A, IMAGE_B),
                )
            if url == first_url:
                return _Fetched(url, "image/jpeg", b"remote-first")
            if url == second_url:
                return _Fetched(url, "image/jpeg", b"remote-wrong")
            raise AssertionError(url)

        timer = _FakeMonotonic()
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=fetch,
            signature_loader=self._signature_loader,
            clock=lambda: NOW,
            monotonic=timer,
            sleep=timer.sleep,
        )

        result = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=1.0,
        )

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        assert result.value is not None
        self.assertFalse(result.value.exact_match)

    def test_gallery_count_mismatch_returns_explicit_mismatch(self) -> None:
        def fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            if url == f"https://www.kleinanzeigen.de/s-anzeige/{AD_ID}":
                return _Fetched(
                    url,
                    "text/html",
                    listing_html(IMAGE_A),
                )
            raise AssertionError("image fetch must not occur for count mismatch")

        timer = _FakeMonotonic()
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=fetch,
            signature_loader=self._signature_loader,
            clock=lambda: NOW,
            monotonic=timer,
            sleep=timer.sleep,
        )
        result = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=1.0,
        )

        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        assert result.value is not None
        self.assertFalse(result.value.exact_match)

    def test_attempt_cap_with_remaining_budget_is_unknown(self) -> None:
        listing_calls = 0

        def fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            nonlocal listing_calls
            if url != f"https://www.kleinanzeigen.de/s-anzeige/{AD_ID}":
                raise AssertionError("count mismatch must not fetch images")
            listing_calls += 1
            return _Fetched(url, "text/html", listing_html(IMAGE_A))

        timer = _FakeMonotonic()
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=fetch,
            signature_loader=self._signature_loader,
            monotonic=timer,
            sleep=timer.sleep,
        )

        with patch("mark_api.private_web_media_verify._MAX_VERIFY_ATTEMPTS", 2):
            result = verifier.verify_media(
                AD_ID,
                self.sources,
                timeout_seconds=10.0,
            )

        self.assertEqual(listing_calls, 2)
        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertEqual(result.error, "private_web_media_verify_attempt_limit")
        self.assertLess(timer.value, 1.0)

    def test_stable_mismatch_polls_until_near_deadline(self) -> None:
        listing_calls = 0

        def fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            nonlocal listing_calls
            if url != f"https://www.kleinanzeigen.de/s-anzeige/{AD_ID}":
                raise AssertionError("count mismatch must not fetch images")
            listing_calls += 1
            return _Fetched(url, "text/html", listing_html(IMAGE_A))

        timer = _FakeMonotonic()
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=fetch,
            signature_loader=self._signature_loader,
            clock=lambda: NOW,
            monotonic=timer,
            sleep=timer.sleep,
        )
        result = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=1.0,
        )

        self.assertGreater(listing_calls, 4)
        self.assertGreaterEqual(timer.value, 0.9)
        self.assertEqual(result.status, ReadStatus.SUCCESS_NONEMPTY)
        assert result.value is not None
        self.assertFalse(result.value.exact_match)

    def test_later_transport_failure_does_not_turn_prior_mismatch_into_mismatch(self) -> None:
        listing_calls = 0

        def fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            nonlocal listing_calls
            if url != f"https://www.kleinanzeigen.de/s-anzeige/{AD_ID}":
                raise AssertionError("count mismatch must not fetch images")
            listing_calls += 1
            if listing_calls == 1:
                return _Fetched(
                    url,
                    "text/html",
                    listing_html(IMAGE_A),
                )
            raise OSError("network down")

        timer = _FakeMonotonic()
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=fetch,
            signature_loader=self._signature_loader,
            clock=lambda: NOW,
            monotonic=timer,
            sleep=timer.sleep,
        )
        result = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=1.0,
        )

        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertIsNone(result.value)

    def test_http_and_parse_uncertainty_fail_closed(self) -> None:
        def http_fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            from mark_api.private_web_media_verify import _MediaHttpStatusError

            raise _MediaHttpStatusError(404)

        http_timer = _FakeMonotonic()
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=http_fetch,
            signature_loader=self._signature_loader,
            monotonic=http_timer,
            sleep=http_timer.sleep,
        )
        result = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=1.0,
        )
        self.assertEqual(result.status, ReadStatus.HTTP_ERROR)
        self.assertEqual(result.http_status, 404)

        def parse_fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            return _Fetched(
                url,
                "text/html",
                listing_html(IMAGE_A, canonical_ad_id="9999999999"),
            )

        parse_timer = _FakeMonotonic()
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=parse_fetch,
            signature_loader=self._signature_loader,
            monotonic=parse_timer,
            sleep=parse_timer.sleep,
        )
        result = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=1.0,
        )
        self.assertEqual(result.status, ReadStatus.PARSE_ERROR)

    def test_default_verifier_bounds_blocked_local_read_with_worker_timeout(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is unavailable on this platform")
        fifo = Path(self.tmp.name) / "blocked.jpg"
        os.mkfifo(fifo)
        verifier = PrivateWebPublicMediaPersistenceVerifier()

        started = time.monotonic()
        result = verifier.verify_media(
            AD_ID,
            (PrivateWebMediaSource(str(fifo)),),
            timeout_seconds=0.2,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(result.status, ReadStatus.TRANSPORT_ERROR)
        self.assertEqual(result.error, "private_web_media_verify_timeout")
        self.assertLess(elapsed, 1.5)

    def test_invalid_contract_inputs_fail_before_network(self) -> None:
        calls = 0

        def fetch(url: str, timeout: float, maximum: int) -> _Fetched:
            nonlocal calls
            calls += 1
            raise AssertionError("network must not be called")

        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=fetch,
            signature_loader=self._signature_loader,
        )

        invalid_id = verifier.verify_media(
            "../bad",
            self.sources,
            timeout_seconds=10.0,
        )
        invalid_timeout = verifier.verify_media(
            AD_ID,
            self.sources,
            timeout_seconds=0,
        )
        invalid_sources = verifier.verify_media(
            AD_ID,
            (),
            timeout_seconds=10.0,
        )

        self.assertEqual(invalid_id.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(invalid_timeout.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(invalid_sources.status, ReadStatus.PARSE_ERROR)
        self.assertEqual(calls, 0)


if __name__ == "__main__":
    unittest.main()
