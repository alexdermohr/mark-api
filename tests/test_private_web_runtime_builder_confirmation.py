from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mark_api.private_web_runtime import (
    PrivateWebContentRuntime,
    PrivateWebInventoryRuntime,
    build_private_web_write_api_runtime,
)
from mark_api.results import ReadResult
from mark_api.storage import SnapshotStore
from mark_api.write_api import WriteApiAccess, WriteCapability


class _OwnerReader:
    def read_ads(self):
        return ReadResult.success_empty(())


class PrivateWebWriteApiRuntimeBuilderConfirmationTests(unittest.TestCase):
    TOKEN = "fake-runtime-token"

    def test_builder_accepts_caller_supplied_confirmation_runtime(self) -> None:
        close_events: list[str] = []
        content_runtime = PrivateWebContentRuntime(
            owner_reader=_OwnerReader(),
            page_factory=lambda: (_ for _ in ()).throw(
                AssertionError("browser page must stay lazy")
            ),
            close_runtime=lambda: close_events.append("content"),
        )
        confirmation_runtime = PrivateWebInventoryRuntime(
            owner_reader=_OwnerReader(),
            close_runtime=lambda: close_events.append("confirmation"),
        )
        access = WriteApiAccess(
            principal="runtime-test",
            bearer_token=self.TOKEN,
            capabilities=frozenset({WriteCapability.CREATE}),
        )
        sentinel = object()

        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            with (
                patch(
                    "mark_api.private_web_runtime.build_private_web_content_runtime",
                    return_value=content_runtime,
                ),
                patch(
                    "mark_api.private_web_runtime.build_private_web_inventory_runtime",
                    side_effect=AssertionError(
                        "confirmation runtime must be caller-supplied"
                    ),
                ) as confirmation_builder,
                patch(
                    "mark_api.private_web_runtime.compose_private_web_write_api_runtime",
                    return_value=sentinel,
                ) as composer,
            ):
                result = build_private_web_write_api_runtime(
                    cdp_port=19610,
                    store=store,
                    access=access,
                    confirmation_runtime=confirmation_runtime,
                    core_writes_enabled=True,
                )

        try:
            self.assertIs(result, sentinel)
            confirmation_builder.assert_not_called()
            composer.assert_called_once()
            self.assertIs(
                composer.call_args.kwargs["confirmation_runtime"],
                confirmation_runtime,
            )
        finally:
            confirmation_runtime.close()
            content_runtime.close()

        self.assertEqual(close_events, ["confirmation", "content"])

    def test_builder_rejects_invalid_confirmation_before_browser_setup(self) -> None:
        access = WriteApiAccess(
            principal="runtime-test",
            bearer_token=self.TOKEN,
            capabilities=frozenset({WriteCapability.CREATE}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "runtime.sqlite")
            with patch(
                "mark_api.private_web_runtime.build_private_web_content_runtime",
                side_effect=AssertionError("browser runtime must not build"),
            ) as builder:
                with self.assertRaisesRegex(
                    TypeError,
                    "confirmation_runtime must be PrivateWebInventoryRuntime or None",
                ):
                    build_private_web_write_api_runtime(
                        cdp_port=19610,
                        store=store,
                        access=access,
                        confirmation_runtime=object(),
                    )
                builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
