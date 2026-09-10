"""All-or-nothing collection, artifact staging, and durable logs-work enqueue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .log_artifact import LogArtifact, LogArtifactError, build_log_artifact
from .log_sync_work import LogSyncWorkError, enqueue_log_sync_work
from .state import StateStore, WorkItem
from .supervisor_logs import (
    SupervisorLogError,
    SupervisorLogTransport,
    collect_supervisor_logs,
)


class LogCollectionError(RuntimeError):
    """A complete required log collection could not be staged and queued safely."""


@dataclass(frozen=True, slots=True)
class LogCollectionResult:
    """Immutable evidence for one complete collected artifact and its durable work item."""

    artifact: LogArtifact
    work: WorkItem


def collect_and_enqueue_supervisor_logs(
    store: StateStore,
    artifact_root: Path,
    target: str,
    *,
    reference_time: datetime,
    token: str | None = None,
    transport: SupervisorLogTransport | None = None,
) -> LogCollectionResult:
    """Collect both required sources before atomically staging and enqueuing exact work."""
    if type(store) is not StateStore:
        raise LogCollectionError("log collection state store is invalid")
    try:
        records = collect_supervisor_logs(
            token=token,
            reference_time=reference_time,
            transport=transport,
        )
        artifact = build_log_artifact(
            artifact_root,
            records,
            reference_time=reference_time,
        )
        work = enqueue_log_sync_work(store, artifact_root, target, artifact.artifact_id)
    except (SupervisorLogError, LogArtifactError, LogSyncWorkError) as exc:
        raise LogCollectionError("required log collection failed closed") from exc
    return LogCollectionResult(artifact=artifact, work=work)
