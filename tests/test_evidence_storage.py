from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import src.evidence_storage as storage


class EvidenceStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "evidence"
        self.root.mkdir()
        self.original_root = storage.EVIDENCE_ROOT
        storage.EVIDENCE_ROOT = self.root

    def tearDown(self) -> None:
        storage.EVIDENCE_ROOT = self.original_root
        self.tmp.cleanup()

    def _write_run(self, run_id: str, created: datetime) -> Path:
        run_dir = self.root / run_id
        run_dir.mkdir()
        (run_dir / "evidence.md").write_text("# evidence\n", encoding="utf-8")
        (run_dir / "request.json").write_text(
            json.dumps({"created_at_utc": created.isoformat()}),
            encoding="utf-8",
        )
        return run_dir

    def test_recognized_directory_requires_markers(self) -> None:
        bad = self.root / "abcdef012345"
        bad.mkdir()
        self.assertFalse(
            storage.is_recognized_evidence_directory(bad, evidence_root=self.root)
        )

    def test_retention_selects_old_runs_only(self) -> None:
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self._write_run("aaaaaaaaaaaa", now - timedelta(days=20))
        self._write_run("bbbbbbbbbbbb", now - timedelta(days=5))
        plan = storage.plan_evidence_cleanup(
            retention_days=14, evidence_root=self.root, now=now
        )
        self.assertEqual([item.run_id for item in plan.candidates], ["aaaaaaaaaaaa"])

    def test_dry_run_does_not_delete(self) -> None:
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        run_dir = self._write_run("cccccccccccc", now - timedelta(days=30))
        plan = storage.plan_evidence_cleanup(
            retention_days=14, evidence_root=self.root, now=now
        )
        with self.assertRaises(ValueError):
            storage.execute_evidence_cleanup(plan, confirm=False)
        self.assertTrue(run_dir.exists())

    def test_confirm_deletes_only_eligible(self) -> None:
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        old = self._write_run("dddddddddddd", now - timedelta(days=30))
        keep = self._write_run("eeeeeeeeeeee", now - timedelta(days=2))
        plan = storage.plan_evidence_cleanup(
            retention_days=14, evidence_root=self.root, now=now
        )
        deleted = storage.execute_evidence_cleanup(plan, confirm=True)
        self.assertEqual(len(deleted), 1)
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())

    def test_symlink_outside_root_is_not_deleted(self) -> None:
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("keep", encoding="utf-8")
        link = self.root / "ffffffffffff"
        os.symlink(outside, link)
        (link / "evidence.md").write_text("x", encoding="utf-8")
        plan = storage.CleanupPlan(
            retention_days=14,
            cutoff_utc=now - timedelta(days=1),
            candidates=(
                storage.EvidenceRunDirectory(
                    run_id="ffffffffffff",
                    path=link,
                    created_at_utc=now - timedelta(days=30),
                    size_bytes=1,
                ),
            ),
        )
        deleted = storage.execute_evidence_cleanup(plan, confirm=True)
        self.assertEqual(deleted, [])
        self.assertTrue(secret.is_file())

    def test_storage_usage_counts_directories(self) -> None:
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self._write_run("111111111111", now)
        self._write_run("222222222222", now)
        report = storage.storage_usage_report(
            database_path=Path(self.tmp.name) / "missing.db",
            evidence_root=self.root,
        )
        self.assertEqual(report.evidence_directory_count, 2)
        self.assertGreater(report.evidence_bytes, 0)


if __name__ == "__main__":
    unittest.main()
