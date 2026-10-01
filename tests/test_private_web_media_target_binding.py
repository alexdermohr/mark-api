from __future__ import annotations

import unittest
from pathlib import Path

from mark_api.private_web import PrivateWebCreateSnapshot, PrivateWebEditorState
from mark_api.private_web_cdp_media import CdpPrivateWebMediaPage


class PrivateWebMediaTargetBindingTests(unittest.TestCase):
    @staticmethod
    def page() -> CdpPrivateWebMediaPage:
        return CdpPrivateWebMediaPage(
            "http://127.0.0.1:19610",
            client_factory=lambda: (_ for _ in ()).throw(
                AssertionError("client must not be created")
            ),
        )

    @staticmethod
    def expected() -> PrivateWebCreateSnapshot:
        return PrivateWebCreateSnapshot(
            state=PrivateWebEditorState.READY,
            title="Neue Vase",
            description="Beschreibung",
            price_amount="12",
        )

    def test_file_input_binding_reuses_exact_media_free_create_contract(self) -> None:
        expression = self.page()._file_input_handle_expression(self.expected())

        for required in (
            'currentOrigin !== "https://www.kleinanzeigen.de"',
            'currentPath !== "/p-anzeige-aufgeben-schritt2.html"',
            "files.length !== 1",
            "files[0].form !== form",
            'posterType.value !== "PRIVATE"',
            'priceType.value !== "FIXED"',
            'offer.value !== "OFFER"',
            'adDraftUuid.value !== ""',
            'adId.value !== ""',
            "hasCaptcha",
            "hasMfa",
            "hasSecurityChallenge",
            "hasLogin",
            "(!false && files[0].files.length !== 0)",
            "(false && files[0].files.length === 0)",
        ):
            self.assertIn(required, expression)

    def test_media_readback_reuses_exact_create_contract_with_nonempty_filelist(self) -> None:
        function = self.page()._media_readback_function(self.expected())

        for required in (
            'currentOrigin !== "https://www.kleinanzeigen.de"',
            'currentPath !== "/p-anzeige-aufgeben-schritt2.html"',
            "files.length !== 1",
            "files[0].form !== form",
            'posterType.value !== "PRIVATE"',
            'priceType.value !== "FIXED"',
            'offer.value !== "OFFER"',
            'adDraftUuid.value !== ""',
            'adId.value !== ""',
            "hasCaptcha",
            "hasMfa",
            "hasSecurityChallenge",
            "hasLogin",
            "(!true && files[0].files.length !== 0)",
            "(true && files[0].files.length === 0)",
            "files[0] !== input",
            "!input.isConnected",
        ):
            self.assertIn(required, function)

    def test_media_opt_in_cannot_enable_existing_create_activation(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "media-filled create activation is not supported",
        ):
            self.page()._create_form_expression(
                expected=self.expected(),
                activation=True,
                allow_media_files=True,
            )

    def test_media_opt_in_is_confined_to_media_cdp_module(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "mark_api"
        base_source = (source_root / "private_web_cdp.py").read_text(
            encoding="utf-8"
        )
        media_source = (source_root / "private_web_cdp_media.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("allow_media_files: bool = False", base_source)
        self.assertNotIn("allow_media_files=True", base_source)
        self.assertEqual(media_source.count("allow_media_files=True"), 2)
        self.assertIn("_media_readback_function", media_source)
        self.assertIn("_media_create_activation_function", media_source)


if __name__ == "__main__":
    unittest.main()