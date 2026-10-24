"""Reproducible Linux FD/MAP_SHARED A6 negative observations.

Synthetic files and owned child processes only. These tests do not establish
cross-UID privilege separation, a complete mount namespace inventory, frozen
host quiescence, protected Browser ACKs, or successful A6 release.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
import io
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import mark_api.a6_host_preflight as preflight
from mark_api.a6_host_preflight import (
    PinnedInode, _fd_writable, _mapping_inode,
    inspect_live_references, pin_source_inodes, main,
)


@unittest.skipUnless(sys.platform == "linux", "Linux /proc required")
class A6HostPreflightNegativeTests(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory(prefix="a6-host-ref-negative-")
        self.addCleanup(root.cleanup)
        self.database = Path(root.name) / "synthetic.sqlite"
        for suffix in ("", "-wal", "-shm"):
            (Path(str(self.database) + suffix)).write_bytes(b"\0" * 4096)
        self.pins = self.enterContext(ExitStack())
        self.pinned = pin_source_inodes(self.database, self.pins)
        self.assertEqual({p.name for p in self.pinned}, {"main", "wal", "shm"})

    def test_executable_diagnostic_never_authorizes_host_cutover(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            rc = main(["--db", str(self.database)])
        payload = json.loads(output.getvalue())
        self.assertEqual(rc, 2)
        self.assertIn(payload["status"], {"blocked", "unattested"})
        # Other users' unreadable host processes correctly yield BLOCKED.
        # This is not an isolated fake /proc and never a release PASS.
        self.assertIs(payload["cutover_attested"], False)
        self.assertIs(payload["release_authorized"], False)
        self.assertNotIn(str(self.database), output.getvalue())

        descriptor = os.open(self.database, os.O_RDWR | os.O_CLOEXEC)
        try:
            with redirect_stdout(io.StringIO()) as ignored:
                rc = main(["--db", str(self.database)])
            blocked = json.loads(ignored.getvalue())
            self.assertEqual(rc, 2)
            self.assertEqual(blocked["status"], "blocked")
            self.assertGreater(blocked["findings_by_kind"]["writable_fd"], 0)
            self.assertIs(blocked["cutover_attested"], False)
        finally:
            os.close(descriptor)

        missing = self.database.with_suffix(".missing")
        output = io.StringIO()
        with redirect_stdout(output):
            rc = main(["--db", str(missing)])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "unknown")
        self.assertNotIn(str(missing), output.getvalue())

    def test_native_writable_descriptor_blocks_even_without_changing_path(self) -> None:
        fd = os.open(self.database, os.O_RDWR | os.O_CLOEXEC)
        try:
            finding = inspect_live_references(self.pinned)
            self.assertTrue(any(
                f.kind == "writable_fd"
                and f.pid == os.getpid() and f.name == "main"
                for f in finding.references
            ))
            self.assertFalse(finding.cutover_attested)
        finally:
            os.close(fd)

    def test_old_writable_mmap_survives_fd_close_and_original_path_rename(self) -> None:
        fd = os.open(self.database, os.O_RDWR | os.O_CLOEXEC)
        try:
            shared = mmap.mmap(fd, 4096, access=mmap.ACCESS_WRITE)
        finally:
            os.close(fd)
        try:
            self.database.rename(self.database.with_suffix(".old"))
            with shared:
                finding = inspect_live_references(self.pinned)
                self.assertTrue(any(
                    f.kind == "writable_shared_mapping"
                    and f.pid == os.getpid() and f.name == "main"
                    for f in finding.references
                ))
                shared[0] = 99
                shared.flush()
            # Merely closing a writable mmap cannot establish that a prior
            # copy of the source was genuine. Never infer A6 release.
            self.assertFalse(inspect_live_references(self.pinned).cutover_attested)
        finally:
            if not shared.closed:
                shared.close()

    def test_wal_shm_old_alias_detected_after_name_switch(self) -> None:
        for name, suffix in (("wal", "-wal"), ("shm", "-shm")):
            with self.subTest(name=name):
                path = Path(str(self.database) + suffix)
                fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
                try:
                    path.rename(Path(str(path) + ".old"))
                    with os.fdopen(os.dup(fd), "r+b", closefd=True):
                        finding = inspect_live_references(self.pinned)
                        self.assertTrue(any(
                            ref.kind == "writable_fd" and ref.name == name
                            for ref in finding.references
                        ))
                finally:
                    os.close(fd)

    def test_child_process_hidden_original_mapping_after_unlink_blocks(self) -> None:
        # An actual other Linux PID has the old inode mapped writable, even
        # after it has closed its FD and the path has been unlinked.
        code = (
            "import mmap,os,sys,time\n"
            "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CLOEXEC)\n"
            "region=mmap.mmap(fd,4096,access=mmap.ACCESS_WRITE)\n"
            "os.close(fd)\n"
            "sys.stdout.write('mapped\\n');sys.stdout.flush()\n"
            "sys.stdin.read(1)\n"
            "region[0]=42;region.flush();region.close()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", code, str(self.database)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(proc.stdout.readline(), "mapped\n")
            self.database.unlink()
            finding = inspect_live_references(self.pinned)
            self.assertTrue(any(
                ref.kind == "writable_shared_mapping"
                and ref.pid == proc.pid and ref.name == "main"
                for ref in finding.references
            ))
            self.assertFalse(finding.cutover_attested)
            assert proc.stdin is not None
            proc.stdin.write("x")
            proc.stdin.flush()
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=4)
            for fd in (proc.stdin, proc.stdout, proc.stderr):
                if fd is not None:
                    fd.close()

    def test_unreadable_process_does_not_produce_safe_absence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="a6-fake-proc-") as temp:
            (Path(temp) / "1234").mkdir()
            finding = inspect_live_references(self.pinned, proc_root=Path(temp))
            self.assertTrue(finding.found_blocker)
            self.assertEqual(finding.references[0].kind, "unknown_process")
            self.assertFalse(finding.cutover_attested)

    def test_no_writable_reference_never_equates_to_released_host(self) -> None:
        result = inspect_live_references(self.pinned)
        self.assertFalse(result.cutover_attested)
        self.assertGreaterEqual(result.examined_pids, 1)

    def test_readonly_fd_and_cow_mapping_do_not_count_as_direct_writers(self) -> None:
        readonly = os.open(self.database, os.O_RDONLY | os.O_CLOEXEC)
        try:
            with mmap.mmap(readonly, 4096, access=mmap.ACCESS_COPY) as private:
                _ = private[0]
                scan = inspect_live_references(self.pinned)
                self.assertFalse(any(
                    ref.pid == os.getpid()
                    and ref.kind in {"writable_fd", "writable_shared_mapping"}
                    and ref.name == "main" for ref in scan.references
                ))
                self.assertFalse(scan.cutover_attested)
        finally:
            os.close(readonly)

    def test_corrupt_fdinfo_or_maps_rejected_not_counted_as_success(self) -> None:
        for row in ("flags: 2", "flags: 0100008", "xyz"):
            with self.subTest(row=row), self.assertRaises(ValueError):
                _fd_writable(row)
        for line in ("garbage", "a-b rw-s 00000000 00:00 bad /test"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                _mapping_inode(line)
        self.assertEqual(
            _mapping_inode(
                "7f000000-7f001000 rw-s 00000000 00:00 0 [anon:synthetic]"
            ), None,
        )

    def test_missing_historical_wal_shm_prevents_pin_not_invented_safe(self) -> None:
        Path(str(self.database) + "-wal").unlink()
        with ExitStack() as stack, self.assertRaises(FileNotFoundError):
            pin_source_inodes(self.database, stack)

    def test_manifest_validation_refuses_partial_or_swapped_generation(self) -> None:
        with self.assertRaises(ValueError):
            inspect_live_references((self.pinned[0],))
        with self.assertRaises(ValueError):
            inspect_live_references(self.pinned + (self.pinned[0],))
        with self.assertRaises(ValueError):
            inspect_live_references((
                PinnedInode("wal", self.pinned[0].device, self.pinned[0].inode),
                self.pinned[1], self.pinned[2],
            ))

    def test_fifo_main_wal_or_shm_returns_sanitized_unknown_without_opening(self) -> None:
        # A normal O_RDONLY open would wait indefinitely for a FIFO writer,
        # before fstat can reject it. Bound each actual child to avoid hanging
        # the suite; execute the installed module file without checkout imports.
        for suffix in ("", "-wal", "-shm"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory(
                prefix="a6-fifo-source-",
            ) as temp:
                database = Path(temp) / "source.sqlite"
                for part in ("", "-wal", "-shm"):
                    path = Path(str(database) + part)
                    if part == suffix:
                        os.mkfifo(path)
                    else:
                        path.write_bytes(b"\0" * 4096)
                completed = subprocess.run(
                    [sys.executable, "-I", preflight.__file__, "--db", str(database)],
                    capture_output=True, text=True, timeout=3, check=False,
                )
                self.assertEqual(completed.returncode, 2)
                report = json.loads(completed.stdout)
                self.assertEqual(report["status"], "unknown")
                self.assertIs(report["cutover_attested"], False)
                self.assertIs(report["release_authorized"], False)
                self.assertEqual(completed.stderr, "")
                self.assertNotIn(str(database), completed.stdout)

    def test_missing_fdinfo_after_protected_fd_stat_remains_unknown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="a6-incomplete-fdinfo-") as temp:
            proc = Path(temp)
            process = proc / "1234"
            (process / "fd").mkdir(parents=True)
            (process / "fdinfo").mkdir()
            (process / "maps").write_text("", encoding="ascii")
            (process / "fd" / "7").symlink_to(self.database)
            report = inspect_live_references(self.pinned, proc_root=proc)
            self.assertTrue(report.found_blocker)
            self.assertTrue(any(
                item.kind == "unknown_process" and item.pid == 1234
                and item.slot == "fdinfo" for item in report.references
            ))
            self.assertFalse(report.cutover_attested)

    def test_manifest_requires_exactly_one_native_identity_per_sqlite_role(self) -> None:
        extra = self.database.with_suffix(".extra")
        extra.write_bytes(b"\0" * 4096)
        descriptor = os.open(extra, os.O_RDONLY | os.O_CLOEXEC)
        try:
            duplicate_role = PinnedInode.from_fd("main", descriptor)
            with self.assertRaises(ValueError):
                inspect_live_references(self.pinned + (duplicate_role,))
        finally:
            os.close(descriptor)
        first = self.pinned[0]
        for invalid in (
            PinnedInode("main", True, first.inode),
            PinnedInode("main", first.device, True),
            PinnedInode("main", str(first.device), first.inode),
            PinnedInode("main", first.device, str(first.inode)),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                inspect_live_references((invalid,) + self.pinned[1:])

    def test_pin_rejects_symlinks_without_following_target(self) -> None:
        target = self.database.with_suffix(".original")
        self.database.rename(target)
        self.database.symlink_to(target)
        with ExitStack() as stack, self.assertRaises((OSError, ValueError)):
            pin_source_inodes(self.database, stack)


if __name__ == "__main__":
    unittest.main()
