"""Pure deterministic identity planning for an existing V2 log artifact layout."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .log_artifact import (
    _MAX_ARTIFACT_BYTES,
    _RETENTION_DAYS,
    LogArtifactError,
    LogRecord,
    _file_evidence,
    _manifest_bytes,
    _normalize_records,
    _normalize_reference_time,
    _render_payloads,
)


def expected_log_artifact_id(
    records: tuple[LogRecord, ...],
    *,
    reference_time: datetime,
) -> str:
    """Compute the exact builder manifest identity without creating filesystem state."""
    reference_utc = _normalize_reference_time(reference_time)
    normalized = _normalize_records(records, reference_utc)
    cutoff = reference_utc - timedelta(days=_RETENTION_DAYS)
    retained = tuple(item for item in normalized if item[1] >= cutoff)
    payloads = _render_payloads(retained)
    files = tuple(_file_evidence(path, payload) for path, payload in sorted(payloads.items()))
    total_size = sum(item.size for item in files)
    manifest = _manifest_bytes(reference_utc, files, retained)
    if total_size + len(manifest) > _MAX_ARTIFACT_BYTES:
        raise LogArtifactError("log artifact exceeds encoded byte limit")
    return hashlib.sha256(manifest).hexdigest()
