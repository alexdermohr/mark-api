from __future__ import annotations

import unittest
from threading import Lock
from unittest.mock import Mock

from mark_api.private_web_runtime import (
    PrivateWebMediaCreateRuntime,
    PrivateWebRuntimeSetupError,
    _SerializedPrivateWebMediaService,
    _SerializedPrivateWebWriteService,
)


class _PendingMediaPage:
    def __init__(self) -> None:
        self.reconciled = 0
        self.closed = 0

    def reconcile_create_media_submit(self) -> None:
        self.reconciled += 1

    def close(self) -> None:
        self.closed += 1


class PrivateWebRuntimeReconciliationFenceTests(unittest.TestCase):
    def test_pending_media_reconciliation_fences_every_composed_write(self) -> None:
        pending = _PendingMediaPage()
        media_runtime = PrivateWebMediaCreateRuntime(
            page_factory=lambda: (_ for _ in ()).throw(
                AssertionError("new media page must not open")
            )
        )
        media_runtime._pending_page = pending
        media_runtime._submit_unknown_fenced = True

        mark_delegate = Mock()
        media_delegate = Mock()
        operation_lock = Lock()
        mark_service = _SerializedPrivateWebWriteService(
            mark_delegate,
            operation_lock,
            media_runtime,
        )
        media_service = _SerializedPrivateWebMediaService(
            media_delegate,
            operation_lock,
            media_runtime,
        )

        blocked_calls = (
            lambda: mark_service.create(object()),
            lambda: mark_service.pause("3524046688"),
            lambda: mark_service.activate("3524046688"),
            lambda: mark_service.delete("3524046688", approval=object()),
            lambda: mark_service.update_content(
                "3524046688",
                title="blocked",
            ),
            lambda: media_service.create_with_media(
                object(),
                ("cover",),
            ),
        )
        for invoke in blocked_calls:
            with self.subTest(invoke=invoke):
                with self.assertRaisesRegex(
                    PrivateWebRuntimeSetupError,
                    "media submit requires reconciliation",
                ):
                    invoke()

        mark_delegate.create.assert_not_called()
        mark_delegate.pause.assert_not_called()
        mark_delegate.activate.assert_not_called()
        mark_delegate.delete.assert_not_called()
        mark_delegate.update_content.assert_not_called()
        media_delegate.create_with_media.assert_not_called()
        self.assertTrue(media_runtime.reconciliation_required)

        media_runtime.reconcile_media_submit()

        self.assertFalse(media_runtime.reconciliation_required)
        self.assertEqual(pending.reconciled, 1)
        self.assertEqual(pending.closed, 1)

        mark_service.pause("3524046688")
        mark_delegate.pause.assert_called_once_with(
            "3524046688",
            authorization_by=None,
            authorization_reference=None,
        )


if __name__ == "__main__":
    unittest.main()
