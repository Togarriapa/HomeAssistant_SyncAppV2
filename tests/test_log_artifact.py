from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.log_artifact import (
    LogArtifact,
    LogArtifactError,
    LogRecord,
    build_log_artifact,
    verify_log_artifact,
)

REFERENCE = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


def _record(
    category: str,
    record_id: str,
    message: str,
    *,
    timestamp: datetime = REFERENCE,
) -> LogRecord:
    return LogRecord(
        category=category,
        record_id=record_id,
        timestamp=timestamp,
        message=message,
    )


def test_builds_deterministic_readme_log_layout(tmp_path: Path) -> None:
    first_staging = tmp_path / "first"
    second_staging = tmp_path / "second"
    first_staging.mkdir(mode=0o700)
    second_staging.mkdir(mode=0o700)
    records = (
        _record("syncapp", "b", "second", timestamp=REFERENCE - timedelta(hours=1)),
        _record("home-assistant", "a", "first"),
        _record("deployments", "d", "deployment"),
        _record("supervisor", "c", "supervisor"),
    )

    first = build_log_artifact(first_staging, records, reference_time=REFERENCE)
    second = build_log_artifact(
        second_staging,
        tuple(reversed(records)),
        reference_time=REFERENCE,
    )

    assert first.artifact_id == second.artifact_id
    assert tuple(item.path for item in first.files) == (
        "logs/deployments/records.jsonl",
        "logs/home-assistant/records.jsonl",
        "logs/supervisor/records.jsonl",
        "logs/syncapp/records.jsonl",
        "manifest.json",
    )
    assert (first.root / "logs/home-assistant/records.jsonl").read_text() == (
        '{"message":"first","record_id":"a","timestamp":"2026-09-10T03:00:00Z"}\n'
    )
    verify_log_artifact(first)
    verify_log_artifact(second)


def test_applies_exact_thirty_day_retention_cutoff(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    cutoff = REFERENCE - timedelta(days=30)
    artifact = build_log_artifact(
        staging,
        (
            _record("syncapp", "kept", "boundary", timestamp=cutoff),
            _record(
                "syncapp",
                "old",
                "expired",
                timestamp=cutoff - timedelta(microseconds=1),
            ),
        ),
        reference_time=REFERENCE,
    )

    rendered = (artifact.root / "logs/syncapp/records.jsonl").read_text()
    assert '"record_id":"kept"' in rendered
    assert '"record_id":"old"' not in rendered


def test_normalizes_timezone_aware_timestamp_to_utc(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    timestamp = datetime.fromisoformat("2026-09-10T04:00:00+01:00")
    artifact = build_log_artifact(
        staging,
        (_record("syncapp", "one", "message", timestamp=timestamp),),
        reference_time=REFERENCE,
    )

    assert '"timestamp":"2026-09-10T03:00:00Z"' in (
        artifact.root / "logs/syncapp/records.jsonl"
    ).read_text()


@pytest.mark.parametrize(
    ("record", "reference_time"),
    [
        (_record("other", "one", "message"), REFERENCE),
        (_record("syncapp", "", "message"), REFERENCE),
        (
            _record(
                "syncapp",
                "one",
                "message",
                timestamp=REFERENCE.replace(tzinfo=None),
            ),
            REFERENCE,
        ),
        (
            _record(
                "syncapp",
                "one",
                "message",
                timestamp=REFERENCE + timedelta(seconds=1),
            ),
            REFERENCE,
        ),
        (_record("syncapp", "one", "message"), REFERENCE.replace(tzinfo=None)),
    ],
)
def test_rejects_invalid_log_metadata(
    tmp_path: Path,
    record: LogRecord,
    reference_time: datetime,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)

    with pytest.raises(LogArtifactError):
        build_log_artifact(staging, (record,), reference_time=reference_time)


def test_rejects_duplicate_record_identity(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    record = _record("syncapp", "duplicate", "message")

    with pytest.raises(LogArtifactError):
        build_log_artifact(staging, (record, record), reference_time=REFERENCE)


def test_enforces_record_and_encoded_byte_limits(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)

    with pytest.raises(LogArtifactError):
        build_log_artifact(
            staging,
            (_record("syncapp", "one", "x" * (2 * 1024 * 1024)),),
            reference_time=REFERENCE,
        )


def test_verification_rejects_content_and_layout_tampering(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    artifact = build_log_artifact(
        staging,
        (_record("syncapp", "one", "message"),),
        reference_time=REFERENCE,
    )
    log_file = artifact.root / "logs/syncapp/records.jsonl"
    log_file.write_text("tampered\n")

    with pytest.raises(LogArtifactError):
        verify_log_artifact(artifact)

    fresh = build_log_artifact(
        staging,
        (_record("syncapp", "two", "message"),),
        reference_time=REFERENCE,
    )
    (fresh.root / "unexpected.txt").write_text("inserted")
    with pytest.raises(LogArtifactError):
        verify_log_artifact(fresh)


def test_verification_rejects_mode_tampering(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    artifact = build_log_artifact(
        staging,
        (_record("syncapp", "one", "message"),),
        reference_time=REFERENCE,
    )
    log_file = artifact.root / "logs/syncapp/records.jsonl"
    log_file.chmod(0o644)

    with pytest.raises(LogArtifactError):
        verify_log_artifact(artifact)


def test_verification_rejects_symlink_and_evidence_tampering(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    artifact = build_log_artifact(
        staging,
        (_record("syncapp", "one", "message"),),
        reference_time=REFERENCE,
    )
    log_file = artifact.root / "logs/syncapp/records.jsonl"
    log_file.unlink()
    os.symlink(artifact.root / "manifest.json", log_file)
    with pytest.raises(LogArtifactError):
        verify_log_artifact(artifact)

    forged = replace(artifact, artifact_id="0" * 64)
    with pytest.raises(LogArtifactError):
        verify_log_artifact(forged)


def test_input_records_are_not_mutated(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    records = (_record("syncapp", "one", "message"),)

    build_log_artifact(staging, records, reference_time=REFERENCE)

    assert records == (_record("syncapp", "one", "message"),)


def test_rejects_forged_artifact_type() -> None:
    with pytest.raises(LogArtifactError):
        verify_log_artifact(LogArtifact)  # type: ignore[arg-type]
