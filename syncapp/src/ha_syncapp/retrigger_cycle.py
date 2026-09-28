"""One scheduler-neutral Retrigger cycle for recovery and candidate detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ha_syncapp.candidate_apply_execution_retrigger import (
    CandidateApplyExecutionRetriggerError,
    CandidateApplyExecutionRetriggerResult,
    run_candidate_apply_execution_retrigger_pass,
)
from ha_syncapp.candidate_apply_retrigger import (
    CandidateApplyRetriggerError,
    CandidateApplyRetriggerResult,
    run_candidate_apply_retrigger_pass,
)
from ha_syncapp.candidate_automation_observation_retrigger import (
    CandidateAutomationObservationRetriggerError,
    CandidateAutomationObservationRetriggerResult,
    run_candidate_automation_observation_retrigger_pass,
)
from ha_syncapp.candidate_backup_retrigger import (
    CandidateBackupRetriggerError,
    CandidateBackupRetriggerResult,
    run_candidate_backup_retrigger_pass,
)
from ha_syncapp.candidate_core_observation_retrigger import (
    CandidateCoreObservationRetriggerError,
    CandidateCoreObservationRetriggerResult,
    run_candidate_core_observation_retrigger_pass,
)
from ha_syncapp.candidate_dependency_retrigger import (
    CandidateDependencyRetriggerError,
    CandidateDependencyRetriggerResult,
    run_candidate_dependency_retrigger_pass,
)
from ha_syncapp.candidate_detection import (
    CandidateDetectionError,
    CandidateDetectionResult,
    detect_and_enqueue_trusted_candidate,
)
from ha_syncapp.candidate_entity_observation_retrigger import (
    CandidateEntityObservationRetriggerError,
    CandidateEntityObservationRetriggerResult,
    run_candidate_entity_observation_retrigger_pass,
)
from ha_syncapp.candidate_fetch_stage_retrigger import (
    CandidateFetchStageRetriggerError,
    CandidateFetchStageRetriggerResult,
    run_candidate_fetch_stage_retrigger_pass,
)
from ha_syncapp.candidate_integration_observation_retrigger import (
    CandidateIntegrationObservationRetriggerError,
    CandidateIntegrationObservationRetriggerResult,
    run_candidate_integration_observation_retrigger_pass,
)
from ha_syncapp.candidate_integrity_retrigger import (
    CandidateIntegrityRetriggerError,
    CandidateIntegrityRetriggerResult,
    run_candidate_integrity_retrigger_pass,
)
from ha_syncapp.candidate_resource_observation_retrigger import (
    CandidateResourceObservationRetriggerError,
    CandidateResourceObservationRetriggerResult,
    run_candidate_resource_observation_retrigger_pass,
)
from ha_syncapp.candidate_restart_retrigger import (
    CandidateRestartRetriggerError,
    CandidateRestartRetriggerResult,
    run_candidate_restart_retrigger_pass,
)
from ha_syncapp.candidate_risk_retrigger import (
    CandidateRiskRetriggerError,
    CandidateRiskRetriggerResult,
    run_candidate_risk_retrigger_pass,
)
from ha_syncapp.candidate_semantic_retrigger import (
    CandidateSemanticRetriggerError,
    CandidateSemanticRetriggerResult,
    run_candidate_semantic_retrigger_pass,
)
from ha_syncapp.candidate_startup_error_observation_retrigger import (
    CandidateStartupErrorObservationRetriggerError,
    CandidateStartupErrorObservationRetriggerResult,
    run_candidate_startup_error_observation_retrigger_pass,
)
from ha_syncapp.candidate_static_retrigger import (
    CandidateStaticRetriggerError,
    CandidateStaticRetriggerResult,
    run_candidate_static_retrigger_pass,
)
from ha_syncapp.candidate_supervisor_observation_retrigger import (
    CandidateSupervisorObservationRetriggerError,
    CandidateSupervisorObservationRetriggerResult,
    run_candidate_supervisor_observation_retrigger_pass,
)
from ha_syncapp.database_retention_work import (
    DatabaseRetentionPassResult,
    DatabaseRetentionWorkError,
    run_database_retention_work_pass,
)
from ha_syncapp.database_sync_retrigger import (
    DatabaseSyncRetriggerError,
    DatabaseSyncRetriggerResult,
    run_database_sync_retrigger_pass,
)
from ha_syncapp.deployment_rollback_retrigger import (
    DeploymentRollbackRetriggerError,
    DeploymentRollbackRetriggerResult,
    run_deployment_rollback_retrigger_pass,
)
from ha_syncapp.github_repo import RepositoryVerificationError
from ha_syncapp.local_sync_retrigger import (
    LocalSyncRetriggerError,
    LocalSyncRetriggerResult,
    run_local_sync_retrigger_pass,
)
from ha_syncapp.log_collection import (
    LogCollectionError,
    LogCollectionResult,
    collect_and_enqueue_supervisor_logs,
)
from ha_syncapp.log_retention_work import (
    LogRetentionPassResult,
    LogRetentionWorkError,
    run_log_retention_work_pass,
)
from ha_syncapp.log_sync_retrigger import (
    LogSyncRetriggerError,
    LogSyncRetriggerResult,
    run_log_sync_retrigger_pass,
)
from ha_syncapp.runtime_sync_retrigger import (
    RuntimeSyncRetriggerError,
    RuntimeSyncRetriggerResult,
    run_runtime_sync_retrigger_pass,
)
from ha_syncapp.state import StateStore


class RetriggerCycleError(RuntimeError):
    """The bounded Retrigger cycle could not complete safely."""


@dataclass(frozen=True, slots=True)
class RetriggerCycleResult:
    """Per-lane evidence from one bounded Retrigger cycle."""

    local_sync: LocalSyncRetriggerResult
    database_sync: DatabaseSyncRetriggerResult
    database_retention: DatabaseRetentionPassResult
    runtime_sync: RuntimeSyncRetriggerResult
    log_sync: LogSyncRetriggerResult
    log_retention: LogRetentionPassResult
    deployment_rollback: DeploymentRollbackRetriggerResult
    candidate_fetch_stage: CandidateFetchStageRetriggerResult
    candidate_integrity: CandidateIntegrityRetriggerResult
    candidate_dependencies: CandidateDependencyRetriggerResult
    candidate_risk: CandidateRiskRetriggerResult
    candidate_static: CandidateStaticRetriggerResult
    candidate_semantic: CandidateSemanticRetriggerResult
    candidate_backup: CandidateBackupRetriggerResult
    candidate_apply: CandidateApplyRetriggerResult
    candidate_apply_execution: CandidateApplyExecutionRetriggerResult
    candidate_restart: CandidateRestartRetriggerResult
    candidate_core_observation: CandidateCoreObservationRetriggerResult
    candidate_supervisor_observation: CandidateSupervisorObservationRetriggerResult
    candidate_integration_observation: CandidateIntegrationObservationRetriggerResult
    candidate_startup_error_observation: CandidateStartupErrorObservationRetriggerResult
    candidate_resource_observation: CandidateResourceObservationRetriggerResult
    candidate_entity_observation: CandidateEntityObservationRetriggerResult
    candidate_automation_observation: CandidateAutomationObservationRetriggerResult
    candidate_detection: CandidateDetectionResult
    log_collection: LogCollectionResult | None


def run_retrigger_cycle(
    store: StateStore,
    home_assistant_root: Path,
    snapshot_staging_root: Path,
    local_workspace_root: Path,
    recorder_database: Path | None,
    database_staging_root: Path,
    database_snapshot_root: Path,
    database_workspace_root: Path,
    runtime_staging_root: Path,
    runtime_snapshot_root: Path,
    runtime_workspace_root: Path,
    target: str,
    github_token: str,
    *,
    core_token: str | None = None,
    log_artifact_root: Path | None = None,
    log_snapshot_root: Path | None = None,
    log_workspace_root: Path | None = None,
    recorder_retention_days: int | None = None,
    retention_reference_time: datetime | None = None,
    recovery_reference_time: datetime | None = None,
    deployment_observation_seconds: int = 300,
) -> RetriggerCycleResult:
    """Recover bounded work, detect candidate, then enqueue one fresh log artifact."""
    if type(store) is not StateStore:
        raise RetriggerCycleError("retrigger cycle state store is invalid")
    log_roots = (log_artifact_root, log_snapshot_root, log_workspace_root)
    if any(root is not None for root in log_roots) and not all(
        root is not None for root in log_roots
    ):
        raise RetriggerCycleError("retrigger logs work roots are incomplete")

    try:
        local_sync = run_local_sync_retrigger_pass(
            store,
            home_assistant_root,
            snapshot_staging_root,
            local_workspace_root,
            target,
            github_token,
        )
        if recorder_database is None:
            database_sync = DatabaseSyncRetriggerResult(recovered_interrupted=0, processed=None)
            database_retention = DatabaseRetentionPassResult(
                recovered_interrupted=0, processed=None
            )
        else:
            database_sync = run_database_sync_retrigger_pass(
                store,
                recorder_database,
                database_staging_root,
                database_snapshot_root,
                database_workspace_root,
                target,
                github_token,
            )
            if recorder_retention_days is None:
                database_retention = DatabaseRetentionPassResult(
                    recovered_interrupted=0, processed=None
                )
            else:
                database_retention = run_database_retention_work_pass(
                    store,
                    database_workspace_root / "retention",
                    target,
                    github_token,
                    retention_days=recorder_retention_days,
                    reference_time=retention_reference_time or datetime.now(UTC),
                )
        runtime_sync = run_runtime_sync_retrigger_pass(
            store,
            runtime_staging_root,
            runtime_snapshot_root,
            runtime_workspace_root,
            target,
            github_token,
            core_token=core_token,
        )
        if log_artifact_root is None:
            log_sync = LogSyncRetriggerResult(recovered_interrupted=0, processed=None)
            log_retention = LogRetentionPassResult(recovered_interrupted=0, processed=None)
        else:
            if log_snapshot_root is None or log_workspace_root is None:
                raise RetriggerCycleError("retrigger logs work roots became incomplete")
            log_sync = run_log_sync_retrigger_pass(
                store,
                log_artifact_root,
                log_snapshot_root,
                log_workspace_root,
                target,
                github_token,
            )
            log_retention = run_log_retention_work_pass(
                store,
                log_workspace_root / "retention",
                target,
                github_token,
                reference_time=datetime.now(UTC),
                recover_interrupted=True,
            )

        deployment_rollback = run_deployment_rollback_retrigger_pass(
            store,
            target,
            github_token,
            core_token,
            reference_time=recovery_reference_time or datetime.now(UTC),
        )

        candidate_fetch_stage = run_candidate_fetch_stage_retrigger_pass(
            store,
            home_assistant_root,
            local_workspace_root / "candidate-fetch",
            snapshot_staging_root / "candidate-stage",
            target,
            github_token,
            reference_time=recovery_reference_time or datetime.now(UTC),
        )
        if candidate_fetch_stage.processed is None:
            candidate_integrity = run_candidate_integrity_retrigger_pass(
                store,
                home_assistant_root,
                local_workspace_root / "candidate-analysis",
                snapshot_staging_root / "candidate-stage",
                github_token,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_integrity = CandidateIntegrityRetriggerResult(0, 0, None)
        if candidate_fetch_stage.processed is None and candidate_integrity.processed is None:
            candidate_dependencies = run_candidate_dependency_retrigger_pass(
                store,
                home_assistant_root,
                snapshot_staging_root / "candidate-stage",
                core_token,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_dependencies = CandidateDependencyRetriggerResult(0, 0, None)
        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
        ):
            candidate_risk = run_candidate_risk_retrigger_pass(
                store,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_risk = CandidateRiskRetriggerResult(0, 0, None)
        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
        ):
            candidate_static = run_candidate_static_retrigger_pass(
                store,
                staging_root=snapshot_staging_root / "candidate-stage",
                home_assistant_root=home_assistant_root,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_static = CandidateStaticRetriggerResult(0, 0, None)
        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
        ):
            candidate_semantic = run_candidate_semantic_retrigger_pass(
                store,
                staging_root=snapshot_staging_root / "candidate-stage",
                home_assistant_root=home_assistant_root,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_semantic = CandidateSemanticRetriggerResult(0, 0, None)

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
        ):
            candidate_backup = run_candidate_backup_retrigger_pass(
                store,
                staging_root=snapshot_staging_root / "candidate-stage",
                home_assistant_root=home_assistant_root,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_backup = CandidateBackupRetriggerResult(0, 0, None)

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
        ):
            candidate_apply = run_candidate_apply_retrigger_pass(
                store,
                staging_root=snapshot_staging_root / "candidate-stage",
                home_assistant_root=home_assistant_root,
                github_token=github_token,
                supervisor_token=None,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_apply = CandidateApplyRetriggerResult(0, 0, None)

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
        ):
            candidate_apply_execution = run_candidate_apply_execution_retrigger_pass(
                store,
                staging_root=snapshot_staging_root / "candidate-stage",
                home_assistant_root=home_assistant_root,
                github_token=github_token,
                supervisor_token=None,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_apply_execution = CandidateApplyExecutionRetriggerResult(0, 0, None)

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
            and candidate_apply_execution.processed is None
        ):
            candidate_restart = run_candidate_restart_retrigger_pass(
                store,
                token=None,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_restart = CandidateRestartRetriggerResult(0, 0, None)

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
            and candidate_apply_execution.processed is None
            and candidate_restart.processed is None
        ):
            candidate_core_observation = run_candidate_core_observation_retrigger_pass(
                store,
                observation_seconds=deployment_observation_seconds,
                token=None,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_core_observation = CandidateCoreObservationRetriggerResult(0, 0, None)

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
            and candidate_apply_execution.processed is None
            and candidate_restart.processed is None
            and candidate_core_observation.processed is None
        ):
            candidate_supervisor_observation = run_candidate_supervisor_observation_retrigger_pass(
                store,
                token=None,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_supervisor_observation = CandidateSupervisorObservationRetriggerResult(
                0, 0, None
            )

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
            and candidate_apply_execution.processed is None
            and candidate_restart.processed is None
            and candidate_core_observation.processed is None
            and candidate_supervisor_observation.processed is None
        ):
            candidate_integration_observation = (
                run_candidate_integration_observation_retrigger_pass(
                    store,
                    token=None,
                    reference_time=recovery_reference_time or datetime.now(UTC),
                )
            )
        else:
            candidate_integration_observation = CandidateIntegrationObservationRetriggerResult(
                0, 0, None
            )

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
            and candidate_apply_execution.processed is None
            and candidate_restart.processed is None
            and candidate_core_observation.processed is None
            and candidate_supervisor_observation.processed is None
            and candidate_integration_observation.processed is None
        ):
            candidate_startup_error_observation = (
                run_candidate_startup_error_observation_retrigger_pass(
                    store,
                    token=None,
                    reference_time=recovery_reference_time or datetime.now(UTC),
                )
            )
        else:
            candidate_startup_error_observation = CandidateStartupErrorObservationRetriggerResult(
                0, 0, None
            )

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
            and candidate_apply_execution.processed is None
            and candidate_restart.processed is None
            and candidate_core_observation.processed is None
            and candidate_supervisor_observation.processed is None
            and candidate_integration_observation.processed is None
            and candidate_startup_error_observation.processed is None
        ):
            candidate_resource_observation = run_candidate_resource_observation_retrigger_pass(
                store,
                token=None,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_resource_observation = CandidateResourceObservationRetriggerResult(0, 0, None)

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
            and candidate_apply_execution.processed is None
            and candidate_restart.processed is None
            and candidate_core_observation.processed is None
            and candidate_supervisor_observation.processed is None
            and candidate_integration_observation.processed is None
            and candidate_startup_error_observation.processed is None
            and candidate_resource_observation.processed is None
        ):
            candidate_entity_observation = run_candidate_entity_observation_retrigger_pass(
                store,
                token=None,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_entity_observation = CandidateEntityObservationRetriggerResult(0, 0, None)

        if (
            candidate_fetch_stage.processed is None
            and candidate_integrity.processed is None
            and candidate_dependencies.processed is None
            and candidate_risk.processed is None
            and candidate_static.processed is None
            and candidate_semantic.processed is None
            and candidate_backup.processed is None
            and candidate_apply.processed is None
            and candidate_apply_execution.processed is None
            and candidate_restart.processed is None
            and candidate_core_observation.processed is None
            and candidate_supervisor_observation.processed is None
            and candidate_integration_observation.processed is None
            and candidate_startup_error_observation.processed is None
            and candidate_resource_observation.processed is None
            and candidate_entity_observation.processed is None
        ):
            candidate_automation_observation = run_candidate_automation_observation_retrigger_pass(
                store,
                token=None,
                reference_time=recovery_reference_time or datetime.now(UTC),
            )
        else:
            candidate_automation_observation = CandidateAutomationObservationRetriggerResult(
                0, 0, None
            )

        candidate_detection = detect_and_enqueue_trusted_candidate(
            store,
            target,
            github_token,
        )

        if log_artifact_root is None:
            log_collection = None
        else:
            log_collection = collect_and_enqueue_supervisor_logs(
                store,
                log_artifact_root,
                target,
                reference_time=datetime.now(UTC),
                token=core_token,
            )
    except (
        LocalSyncRetriggerError,
        DatabaseSyncRetriggerError,
        RuntimeSyncRetriggerError,
        LogSyncRetriggerError,
        LogRetentionWorkError,
        CandidateDetectionError,
        RepositoryVerificationError,
        LogCollectionError,
        DatabaseRetentionWorkError,
        DeploymentRollbackRetriggerError,
        CandidateFetchStageRetriggerError,
        CandidateIntegrityRetriggerError,
        CandidateDependencyRetriggerError,
        CandidateRiskRetriggerError,
        CandidateStaticRetriggerError,
        CandidateSemanticRetriggerError,
        CandidateBackupRetriggerError,
        CandidateApplyRetriggerError,
        CandidateApplyExecutionRetriggerError,
        CandidateRestartRetriggerError,
        CandidateCoreObservationRetriggerError,
        CandidateSupervisorObservationRetriggerError,
        CandidateIntegrationObservationRetriggerError,
        CandidateStartupErrorObservationRetriggerError,
        CandidateResourceObservationRetriggerError,
        CandidateEntityObservationRetriggerError,
        CandidateAutomationObservationRetriggerError,
    ) as exc:
        raise RetriggerCycleError("retrigger cycle failed closed") from exc

    return RetriggerCycleResult(
        local_sync=local_sync,
        database_sync=database_sync,
        database_retention=database_retention,
        runtime_sync=runtime_sync,
        log_sync=log_sync,
        log_retention=log_retention,
        deployment_rollback=deployment_rollback,
        candidate_fetch_stage=candidate_fetch_stage,
        candidate_integrity=candidate_integrity,
        candidate_dependencies=candidate_dependencies,
        candidate_risk=candidate_risk,
        candidate_static=candidate_static,
        candidate_semantic=candidate_semantic,
        candidate_backup=candidate_backup,
        candidate_apply=candidate_apply,
        candidate_apply_execution=candidate_apply_execution,
        candidate_restart=candidate_restart,
        candidate_core_observation=candidate_core_observation,
        candidate_supervisor_observation=candidate_supervisor_observation,
        candidate_integration_observation=candidate_integration_observation,
        candidate_startup_error_observation=candidate_startup_error_observation,
        candidate_resource_observation=candidate_resource_observation,
        candidate_entity_observation=candidate_entity_observation,
        candidate_automation_observation=candidate_automation_observation,
        candidate_detection=candidate_detection,
        log_collection=log_collection,
    )
