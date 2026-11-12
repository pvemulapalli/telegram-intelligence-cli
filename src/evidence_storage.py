from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import DATABASE_PATH
from src.evidence import EVIDENCE_ROOT

RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{12}$")
DEFAULT_RETENTION_DAYS = 14


@dataclass(frozen=True)
class StorageUsageReport:
    database_path: Path
    database_bytes: int
    evidence_root: Path
    evidence_directory_count: int
    evidence_bytes: int


@dataclass(frozen=True)
class EvidenceRunDirectory:
    run_id: str
    path: Path
    created_at_utc: datetime
    size_bytes: int


@dataclass(frozen=True)
class CleanupPlan:
    retention_days: int
    cutoff_utc: datetime
    candidates: tuple[EvidenceRunDirectory, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.candidates)


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _parse_created_at(run_dir: Path) -> datetime | None:
    request_path = run_dir / "request.json"
    if request_path.is_file():
        try:
            data = json.loads(request_path.read_text(encoding="utf-8"))
            raw = data.get("created_at_utc")
            if raw:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    try:
        ts = run_dir.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except OSError:
        return None


def is_recognized_evidence_directory(path: Path, *, evidence_root: Path) -> bool:
    if not path.is_dir():
        return False
    if not RUN_ID_PATTERN.fullmatch(path.name):
        return False
    if not (path / "request.json").is_file():
        return False
    if not (path / "evidence.md").is_file():
        return False
    return _is_safe_evidence_path(path, evidence_root=evidence_root)


def _is_safe_evidence_path(path: Path, *, evidence_root: Path) -> bool:
    root = evidence_root.resolve()
    candidate = path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    if path.is_symlink():
        target = path.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return False
    return True


def list_evidence_runs(evidence_root: Path = EVIDENCE_ROOT) -> list[EvidenceRunDirectory]:
    if not evidence_root.is_dir():
        return []
    runs: list[EvidenceRunDirectory] = []
    for entry in evidence_root.iterdir():
        if not is_recognized_evidence_directory(entry, evidence_root=evidence_root):
            continue
        created = _parse_created_at(entry)
        if created is None:
            continue
        runs.append(
            EvidenceRunDirectory(
                run_id=entry.name,
                path=entry,
                created_at_utc=created,
                size_bytes=_dir_size(entry),
            )
        )
    runs.sort(key=lambda r: r.created_at_utc)
    return runs


def storage_usage_report(
    *,
    database_path: Path = DATABASE_PATH,
    evidence_root: Path = EVIDENCE_ROOT,
) -> StorageUsageReport:
    db_bytes = database_path.stat().st_size if database_path.is_file() else 0
    runs = list_evidence_runs(evidence_root)
    evidence_bytes = sum(r.size_bytes for r in runs)
    return StorageUsageReport(
        database_path=database_path,
        database_bytes=db_bytes,
        evidence_root=evidence_root,
        evidence_directory_count=len(runs),
        evidence_bytes=evidence_bytes,
    )


def plan_evidence_cleanup(
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    evidence_root: Path = EVIDENCE_ROOT,
    now: datetime | None = None,
) -> CleanupPlan:
    anchor = now or datetime.now(timezone.utc)
    cutoff = anchor - timedelta(days=retention_days)
    candidates = [
        run
        for run in list_evidence_runs(evidence_root)
        if run.created_at_utc < cutoff
    ]
    return CleanupPlan(
        retention_days=retention_days,
        cutoff_utc=cutoff.replace(microsecond=0),
        candidates=tuple(candidates),
    )


def execute_evidence_cleanup(plan: CleanupPlan, *, confirm: bool) -> list[Path]:
    if not confirm:
        raise ValueError("Cleanup requires confirm=True")
    deleted: list[Path] = []
    root = EVIDENCE_ROOT.resolve()
    for item in plan.candidates:
        if not _is_safe_evidence_path(item.path, evidence_root=root):
            continue
        if not is_recognized_evidence_directory(item.path, evidence_root=root):
            continue
        shutil.rmtree(item.path)
        deleted.append(item.path)
    return deleted


def format_bytes(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KiB"
    return f"{num / (1024 * 1024):.1f} MiB"


def print_storage_usage(report: StorageUsageReport) -> None:
    print("Local storage usage")
    print(f"  Database: {report.database_path}")
    print(f"    size: {format_bytes(report.database_bytes)}")
    print(f"  Evidence exports: {report.evidence_root}")
    print(f"    directories: {report.evidence_directory_count}")
    print(f"    total size: {format_bytes(report.evidence_bytes)}")


def print_cleanup_plan(plan: CleanupPlan, *, dry_run: bool) -> None:
    mode = "Preview" if dry_run else "Deleting"
    print(f"{mode} evidence cleanup (retention: {plan.retention_days} days)")
    print(f"  Cutoff (UTC): {plan.cutoff_utc.isoformat()}")
    if not plan.candidates:
        print("  No evidence directories eligible for removal.")
        return
    print(f"  Directories: {len(plan.candidates)}")
    print(f"  Space recovered: {format_bytes(plan.total_bytes)}")
    for item in plan.candidates:
        created = item.created_at_utc.replace(microsecond=0).isoformat()
        print(
            f"    {item.run_id}  created={created}  "
            f"size={format_bytes(item.size_bytes)}  path={item.path}"
        )
    if dry_run:
        print("  Re-run with --confirm to delete these directories.")
