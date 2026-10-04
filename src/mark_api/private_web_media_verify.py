from __future__ import annotations

import io
import json
import math
import multiprocessing
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .private_web_media import (
    PrivateWebMediaPersistenceSnapshot,
    PrivateWebMediaSource,
)
from .results import ReadResult, ReadStatus


_LISTING_ORIGIN = "https://www.kleinanzeigen.de"
_IMAGE_ORIGIN = "https://img.kleinanzeigen.de"
_MAX_LISTING_BYTES = 2 * 1024 * 1024
_MAX_REMOTE_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_LOCAL_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_IMAGE_PIXELS = 60_000_000
_MAX_GALLERY_IMAGES = 32
_MAX_VERIFY_ATTEMPTS = 128
_RETRY_DELAY_SECONDS = 0.25
_FINAL_OBSERVATION_RESERVE_SECONDS = 0.05
_DEADLINE_EPSILON_SECONDS = 1e-9
_NORMALIZED_SIZE = (64, 64)
_MAX_ASPECT_RATIO_LOG_DELTA = 0.02
_MAX_RGB_MEAN_ABS_ERROR = 3.0
_MAX_RGB_P99_ABS_ERROR = 16
_MAX_RGB_MAX_ABS_ERROR = 32

_AD_ID_RE = re.compile(r"^[0-9]{1,32}$")
_IMAGE_PATH_RE = re.compile(
    r"^/api/v1/prod-ads/images/"
    r"(?P<prefix>[0-9a-f]{2})/"
    r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})$"
)


class _MediaVerifierError(Exception):
    pass


class _MediaHttpStatusError(_MediaVerifierError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"http status {status}")


class _MediaParseError(_MediaVerifierError):
    pass


class _MediaDecodeError(_MediaVerifierError):
    pass


class _MediaFetchDeadlineTimeout(TimeoutError):
    """The normal public-Web fetch exhausted its caller-supplied time budget."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class _Fetched:
    final_url: str
    content_type: str
    body: bytes


@dataclass(frozen=True, slots=True)
class _ImageSignature:
    width: int
    height: int
    normalized_rgb: bytes

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height


Fetch = Callable[[str, float, int], _Fetched]
SignatureLoader = Callable[[bytes], _ImageSignature]


class _ListingGalleryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_urls: list[str] = []
        self.gallery_count = 0
        self.gallery_image_urls: list[str] = []
        self._div_depth = 0
        self._gallery_depth: int | None = None
        self._script_active = False
        self._script_parts: list[str] = []

    @staticmethod
    def _attrs(items: list[tuple[str, str | None]]) -> dict[str, str]:
        return {
            key: value
            for key, value in items
            if isinstance(key, str) and isinstance(value, str)
        }

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = self._attrs(attrs)
        if tag == "link":
            rel = set(values.get("rel", "").lower().split())
            if "canonical" in rel and values.get("href"):
                self.canonical_urls.append(values["href"])

        if tag == "div":
            self._div_depth += 1
            classes = set(values.get("class", "").split())
            if (
                "vip-image-gallery" in classes
                and "j-gallery-image" in classes
            ):
                self.gallery_count += 1
                if self._gallery_depth is None:
                    self._gallery_depth = self._div_depth

        if (
            self._gallery_depth is not None
            and tag == "script"
            and values.get("type", "").lower() == "application/ld+json"
        ):
            self._script_active = True
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._script_active:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_active:
            self._script_active = False
            raw = "".join(self._script_parts).strip()
            self._script_parts = []
            if raw:
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError):
                    return
                if (
                    isinstance(value, dict)
                    and value.get("@type") == "ImageObject"
                    and isinstance(value.get("contentUrl"), str)
                ):
                    self.gallery_image_urls.append(value["contentUrl"])

        if tag == "div":
            if self._gallery_depth == self._div_depth:
                self._gallery_depth = None
            if self._div_depth > 0:
                self._div_depth -= 1


def _validate_ad_id(ad_id: str) -> str:
    if not isinstance(ad_id, str) or _AD_ID_RE.fullmatch(ad_id) is None:
        raise ValueError("ad_id must be a numeric Kleinanzeigen ID")
    return ad_id


def _canonical_url_matches_ad(url: str, ad_id: str) -> bool:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.kleinanzeigen.de"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return False
    parts = [item for item in parsed.path.split("/") if item]
    if parts == ["s-anzeige", ad_id]:
        return True
    return (
        len(parts) == 3
        and parts[0] == "s-anzeige"
        and parts[1]
        and parts[2].startswith(ad_id + "-")
    )


def _trusted_image_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "img.kleinanzeigen.de"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _MediaParseError("untrusted gallery image URL")
    match = _IMAGE_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise _MediaParseError("invalid gallery image path")
    if match.group("uuid")[:2] != match.group("prefix"):
        raise _MediaParseError("gallery image prefix mismatch")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) != {"rule"} or len(query["rule"]) != 1:
        raise _MediaParseError("invalid gallery image query")
    return f"{_IMAGE_ORIGIN}{parsed.path}?rule=$_57.JPG"


def _parse_listing_gallery(body: bytes, ad_id: str) -> tuple[str, ...]:
    try:
        html = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _MediaParseError("listing is not UTF-8") from exc

    parser = _ListingGalleryParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise _MediaParseError("listing HTML parse failed") from exc

    if parser.gallery_count != 1:
        raise _MediaParseError("listing gallery is not unique")
    if len(parser.canonical_urls) != 1:
        raise _MediaParseError("listing canonical URL is not unique")
    if not _canonical_url_matches_ad(parser.canonical_urls[0], ad_id):
        raise _MediaParseError("listing canonical ID mismatch")
    if (
        not parser.gallery_image_urls
        or len(parser.gallery_image_urls) > _MAX_GALLERY_IMAGES
    ):
        raise _MediaParseError("listing gallery image count is invalid")

    urls = tuple(_trusted_image_url(item) for item in parser.gallery_image_urls)
    identities = tuple(urlsplit(item).path for item in urls)
    if len(set(identities)) != len(identities):
        raise _MediaParseError("listing gallery contains duplicate image IDs")
    return urls


def _default_fetch(url: str, timeout_seconds: float, max_bytes: int) -> _Fetched:
    if timeout_seconds <= 0:
        raise TimeoutError("fetch timeout exhausted")
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Mark/0.1)",
            "Accept": "text/html,image/jpeg;q=0.9,*/*;q=0.1",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.getcode())
            if status != 200:
                raise _MediaHttpStatusError(status)
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    declared = int(raw_length, 10)
                except ValueError as exc:
                    raise _MediaParseError("invalid content length") from exc
                if declared < 0 or declared > max_bytes:
                    raise _MediaParseError("response exceeds size limit")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise _MediaParseError("response exceeds size limit")
            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            return _Fetched(
                final_url=response.geturl(),
                content_type=content_type,
                body=body,
            )
    except HTTPError as exc:
        raise _MediaHttpStatusError(int(exc.code)) from None
    except TimeoutError as exc:
        raise _MediaFetchDeadlineTimeout(
            "media verifier fetch exhausted its time budget"
        ) from exc
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise _MediaFetchDeadlineTimeout(
                "media verifier fetch exhausted its time budget"
            ) from exc
        raise OSError("media verifier transport failed") from exc


def _read_local_source(source: PrivateWebMediaSource) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source.path, flags)
    except OSError as exc:
        raise _MediaDecodeError("expected media is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAX_LOCAL_IMAGE_BYTES
        ):
            raise _MediaDecodeError("expected media is invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise _MediaDecodeError("expected media read was incomplete")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise _MediaDecodeError("expected media changed during verification")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _pillow_signature(data: bytes) -> _ImageSignature:
    try:
        from PIL import Image, ImageOps
    except ModuleNotFoundError as exc:
        raise _MediaDecodeError("Pillow is unavailable") from exc

    try:
        with Image.open(io.BytesIO(data)) as opened:
            if getattr(opened, "n_frames", 1) != 1:
                raise _MediaDecodeError("animated media is not verifiable")
            width, height = opened.size
            if (
                not isinstance(width, int)
                or not isinstance(height, int)
                or width <= 0
                or height <= 0
                or width * height > _MAX_IMAGE_PIXELS
            ):
                raise _MediaDecodeError("image dimensions are invalid")
            opened.load()
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            bands = image.getbands()
            if "transparency" in opened.info:
                raise _MediaDecodeError(
                    "transparent media is not exactly verifiable"
                )
            if "A" in bands:
                alpha = image.getchannel("A")
                minimum, _maximum = alpha.getextrema()
                if minimum < 255:
                    raise _MediaDecodeError(
                        "transparent media is not exactly verifiable"
                    )
            rgb = image.convert("RGB")
            normalized = rgb.resize(
                _NORMALIZED_SIZE,
                resample=Image.Resampling.LANCZOS,
            )
            normalized_rgb = normalized.tobytes()

            return _ImageSignature(
                width=width,
                height=height,
                normalized_rgb=normalized_rgb,
            )
    except _MediaDecodeError:
        raise
    except Exception as exc:
        raise _MediaDecodeError("image decode failed") from exc


def _signatures_match(
    expected: _ImageSignature,
    observed: _ImageSignature,
) -> bool:
    if (
        expected.width <= 0
        or expected.height <= 0
        or observed.width <= 0
        or observed.height <= 0
        or len(expected.normalized_rgb) != len(observed.normalized_rgb)
        or not expected.normalized_rgb
    ):
        return False

    ratio_delta = abs(
        math.log(expected.aspect_ratio / observed.aspect_ratio)
    )
    if ratio_delta > _MAX_ASPECT_RATIO_LOG_DELTA:
        return False

    differences = sorted(
        abs(left - right)
        for left, right in zip(
            expected.normalized_rgb,
            observed.normalized_rgb,
            strict=True,
        )
    )
    mean_error = sum(differences) / len(differences)
    if mean_error > _MAX_RGB_MEAN_ABS_ERROR:
        return False
    p99_index = min(
        len(differences) - 1,
        int(len(differences) * 0.99),
    )
    return (
        differences[p99_index] <= _MAX_RGB_P99_ABS_ERROR
        and differences[-1] <= _MAX_RGB_MAX_ABS_ERROR
    )


def _has_exact_matching(
    expected: tuple[_ImageSignature, ...],
    observed: tuple[_ImageSignature, ...],
) -> bool:
    return (
        len(expected) == len(observed)
        and bool(expected)
        and all(
            _signatures_match(expected_item, observed_item)
            for expected_item, observed_item in zip(
                expected,
                observed,
                strict=True,
            )
        )
    )


class PrivateWebPublicMediaPersistenceVerifier:
    """Read-only public-Web confirmation for media on one exact listing.

    The public VIP page is normal Kleinanzeigen Web UI. Only its own gallery is
    considered; recommendation images are ignored. Server-side CDN variants
    are recompressed/resized, so identity uses a conservative content
    fingerprint rather than byte equality.
    """

    def __init__(
        self,
        *,
        fetch: Fetch | None = None,
        signature_loader: SignatureLoader | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._fetch = fetch or _default_fetch
        self._signature_loader = signature_loader or _pillow_signature
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._sleep = sleep
        self._use_isolated_default = (
            fetch is None
            and signature_loader is None
            and clock is None
            and monotonic is time.monotonic
            and sleep is time.sleep
        )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("media verification timeout")
        return remaining

    def _listing(
        self,
        ad_id: str,
        *,
        deadline: float,
    ) -> tuple[str, ...]:
        response = self._fetch(
            f"{_LISTING_ORIGIN}/s-anzeige/{ad_id}",
            self._remaining(deadline),
            _MAX_LISTING_BYTES,
        )
        if response.content_type != "text/html":
            raise _MediaParseError("listing content type is invalid")
        if not _canonical_url_matches_ad(response.final_url, ad_id):
            raise _MediaParseError("listing response URL is not target-bound")
        return _parse_listing_gallery(response.body, ad_id)

    def _observed_signatures(
        self,
        urls: tuple[str, ...],
        *,
        deadline: float,
    ) -> tuple[_ImageSignature, ...]:
        signatures: list[_ImageSignature] = []
        for url in urls:
            response = self._fetch(
                url,
                self._remaining(deadline),
                _MAX_REMOTE_IMAGE_BYTES,
            )
            if response.content_type != "image/jpeg":
                raise _MediaParseError("gallery image content type is invalid")
            if _trusted_image_url(response.final_url) != url:
                raise _MediaParseError("gallery image redirect is untrusted")
            signatures.append(self._signature_loader(response.body))
        return tuple(signatures)

    def verify_media(
        self,
        ad_id: str,
        expected_sources: tuple[PrivateWebMediaSource, ...],
        *,
        timeout_seconds: float,
    ) -> ReadResult[PrivateWebMediaPersistenceSnapshot]:
        if self._use_isolated_default:
            return _verify_media_in_isolated_process(
                ad_id,
                expected_sources,
                timeout_seconds=timeout_seconds,
            )
        return self._verify_media_inline(
            ad_id,
            expected_sources,
            timeout_seconds=timeout_seconds,
        )

    def _verify_media_inline(
        self,
        ad_id: str,
        expected_sources: tuple[PrivateWebMediaSource, ...],
        *,
        timeout_seconds: float,
    ) -> ReadResult[PrivateWebMediaPersistenceSnapshot]:
        try:
            target_ad_id = _validate_ad_id(ad_id)
        except (TypeError, ValueError):
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="invalid_media_verifier_ad_id",
            )
        if (
            not isinstance(expected_sources, tuple)
            or not expected_sources
            or len(expected_sources) > _MAX_GALLERY_IMAGES
            or any(
                not isinstance(source, PrivateWebMediaSource)
                for source in expected_sources
            )
        ):
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="invalid_expected_media",
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="invalid_media_verifier_timeout",
            )

        deadline = self._monotonic() + float(timeout_seconds)
        try:
            expected = tuple(
                self._signature_loader(_read_local_source(source))
                for source in expected_sources
            )
        except _MediaDecodeError:
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="expected_media_unverifiable",
            )
        except Exception:
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="expected_media_read_failed",
            )

        last_http_status: int | None = None
        terminal_observation = "unknown"
        attempt_limit_exhausted = False
        last_complete_mismatch_seconds: float | None = None
        for attempt in range(_MAX_VERIFY_ATTEMPTS):
            if (
                attempt > 0
                and terminal_observation == "mismatch"
                and last_complete_mismatch_seconds is not None
            ):
                try:
                    remaining = self._remaining(deadline)
                except TimeoutError:
                    break
                minimum_retry_budget = (
                    last_complete_mismatch_seconds
                    + _FINAL_OBSERVATION_RESERVE_SECONDS
                )
                if (
                    remaining
                    <= minimum_retry_budget + _DEADLINE_EPSILON_SECONDS
                ):
                    break

            attempt_started = self._monotonic()
            try:
                urls = self._listing(target_ad_id, deadline=deadline)
                if len(urls) != len(expected):
                    terminal_observation = "mismatch"
                    last_complete_mismatch_seconds = max(
                        0.0,
                        self._monotonic() - attempt_started,
                    )
                else:
                    observed = self._observed_signatures(
                        urls,
                        deadline=deadline,
                    )
                    if _has_exact_matching(expected, observed):
                        observed_at = self._clock()
                        if (
                            observed_at.tzinfo is None
                            or observed_at.utcoffset() is None
                        ):
                            raise _MediaParseError(
                                "media verifier clock is naive"
                            )
                        return ReadResult.success_nonempty(
                            PrivateWebMediaPersistenceSnapshot(
                                ad_id=target_ad_id,
                                observed_at=observed_at,
                                source="private-web-public-detail-gallery",
                                exact_match=True,
                            )
                        )
                    terminal_observation = "mismatch"
                    last_complete_mismatch_seconds = max(
                        0.0,
                        self._monotonic() - attempt_started,
                    )
            except _MediaHttpStatusError as exc:
                last_http_status = exc.status
                terminal_observation = "http_error"
            except _MediaFetchDeadlineTimeout:
                if not (
                    terminal_observation == "mismatch"
                    and last_complete_mismatch_seconds is not None
                ):
                    terminal_observation = "unknown"
                break
            except TimeoutError:
                if not (
                    terminal_observation == "mismatch"
                    and last_complete_mismatch_seconds is not None
                    and (
                        deadline - self._monotonic()
                        <= _DEADLINE_EPSILON_SECONDS
                    )
                ):
                    terminal_observation = "unknown"
                break
            except (_MediaParseError, _MediaDecodeError):
                terminal_observation = "parse_error"
            except Exception:
                terminal_observation = "transport_error"

            if attempt == _MAX_VERIFY_ATTEMPTS - 1:
                try:
                    remaining = self._remaining(deadline)
                except TimeoutError:
                    break
                if (
                    remaining
                    > _FINAL_OBSERVATION_RESERVE_SECONDS
                    + _DEADLINE_EPSILON_SECONDS
                ):
                    attempt_limit_exhausted = True
                    terminal_observation = "unknown"
                break

            try:
                remaining = self._remaining(deadline)
            except TimeoutError:
                break
            retry_reserve = _FINAL_OBSERVATION_RESERVE_SECONDS
            if (
                terminal_observation == "mismatch"
                and last_complete_mismatch_seconds is not None
            ):
                retry_reserve += last_complete_mismatch_seconds
            if (
                remaining
                <= retry_reserve + _DEADLINE_EPSILON_SECONDS
            ):
                break
            delay = min(
                _RETRY_DELAY_SECONDS,
                max(
                    0.0,
                    remaining - retry_reserve,
                ),
            )
            if delay <= 0:
                break
            try:
                self._sleep(delay)
            except Exception:
                terminal_observation = "unknown"
                break

        if attempt_limit_exhausted:
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="private_web_media_verify_attempt_limit",
            )
        if terminal_observation == "mismatch":
            observed_at = self._clock()
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                return ReadResult.failure(
                    ReadStatus.PARSE_ERROR,
                    error="private_web_media_verifier_clock_invalid",
                )
            return ReadResult.success_nonempty(
                PrivateWebMediaPersistenceSnapshot(
                    ad_id=target_ad_id,
                    observed_at=observed_at,
                    source="private-web-public-detail-gallery",
                    exact_match=False,
                )
            )
        if (
            terminal_observation == "http_error"
            and last_http_status is not None
        ):
            return ReadResult.failure(
                ReadStatus.HTTP_ERROR,
                error="private_web_media_http_error",
                http_status=last_http_status,
            )
        if terminal_observation == "parse_error":
            return ReadResult.failure(
                ReadStatus.PARSE_ERROR,
                error="private_web_media_read_unverifiable",
            )
        if terminal_observation == "transport_error":
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="private_web_media_read_failed",
            )
        return ReadResult.failure(
            ReadStatus.TRANSPORT_ERROR,
            error="private_web_media_verify_timeout",
        )

_WORKER_CLEANUP_SECONDS = 0.25
_WORKER_RESULT_GRACE_SECONDS = 0.10


def _default_verification_worker(
    connection,
    ad_id: str,
    source_paths: tuple[str, ...],
    deadline_monotonic: float,
) -> None:
    try:
        sources = tuple(
            PrivateWebMediaSource(path)
            for path in source_paths
        )
        verifier = PrivateWebPublicMediaPersistenceVerifier(
            fetch=_default_fetch,
            signature_loader=_pillow_signature,
        )
        remaining = (
            deadline_monotonic
            - time.monotonic()
            - _WORKER_RESULT_GRACE_SECONDS
        )
        if remaining <= 0:
            result = ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="private_web_media_verify_timeout",
            )
        else:
            result = verifier._verify_media_inline(
                ad_id,
                sources,
                timeout_seconds=remaining,
            )
        connection.send(result)
    except BaseException:
        try:
            connection.send(
                ReadResult.failure(
                    ReadStatus.TRANSPORT_ERROR,
                    error="private_web_media_worker_failed",
                )
            )
        except Exception:
            pass
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _verify_media_in_isolated_process(
    ad_id: str,
    expected_sources: tuple[PrivateWebMediaSource, ...],
    *,
    timeout_seconds: float,
) -> ReadResult[PrivateWebMediaPersistenceSnapshot]:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        return ReadResult.failure(
            ReadStatus.PARSE_ERROR,
            error="invalid_media_verifier_timeout",
        )
    if (
        not isinstance(expected_sources, tuple)
        or not expected_sources
        or len(expected_sources) > _MAX_GALLERY_IMAGES
        or any(
            not isinstance(source, PrivateWebMediaSource)
            for source in expected_sources
        )
    ):
        return ReadResult.failure(
            ReadStatus.PARSE_ERROR,
            error="invalid_expected_media",
        )
    try:
        _validate_ad_id(ad_id)
    except (TypeError, ValueError):
        return ReadResult.failure(
            ReadStatus.PARSE_ERROR,
            error="invalid_media_verifier_ad_id",
        )

    deadline = time.monotonic() + float(timeout_seconds)
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_default_verification_worker,
        args=(
            sending,
            ad_id,
            tuple(source.path for source in expected_sources),
            deadline,
        ),
        daemon=True,
    )
    try:
        process.start()
    except Exception:
        receiving.close()
        sending.close()
        return ReadResult.failure(
            ReadStatus.TRANSPORT_ERROR,
            error="private_web_media_worker_start_failed",
        )
    sending.close()

    remaining = max(0.0, deadline - time.monotonic())
    process.join(remaining)
    if process.is_alive():
        process.terminate()
        process.join(_WORKER_CLEANUP_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(_WORKER_CLEANUP_SECONDS)
        receiving.close()
        return ReadResult.failure(
            ReadStatus.TRANSPORT_ERROR,
            error="private_web_media_verify_timeout",
        )

    try:
        if process.exitcode != 0 or not receiving.poll():
            return ReadResult.failure(
                ReadStatus.TRANSPORT_ERROR,
                error="private_web_media_worker_failed",
            )
        result = receiving.recv()
    except Exception:
        return ReadResult.failure(
            ReadStatus.TRANSPORT_ERROR,
            error="private_web_media_worker_failed",
        )
    finally:
        receiving.close()

    if not isinstance(result, ReadResult):
        return ReadResult.failure(
            ReadStatus.TRANSPORT_ERROR,
            error="private_web_media_worker_failed",
        )
    return result
