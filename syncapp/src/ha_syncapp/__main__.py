"""App lifecycle with fail-closed Repo B trust and same-owner Retrigger IPC."""

import argparse
import json
import os
import signal
import stat
import sys
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from . import __version__
from .administrative_retry_request import (
    AdministrativeRetryRequestError,
    apply_administrative_retry_request,
)
from .candidate_deploy_key_ingress import (
    CandidateDeployKeyIngressError,
    DeployKeyCandidateIngress,
)
from .candidate_detection_service import (
    CandidateDetectionService,
    CandidateDetectionServiceError,
)
from .config import Config, ConfigError, load_config
from .database_startup import (
    DatabaseStartupError,
    DatabaseStartupResult,
    run_startup_database_sync,
)
from .database_sync_service import DatabaseSyncService, DatabaseSyncServiceError
from .deploy_key_access import DeployKeyAccessError, test_repo_b_deploy_key_access
from .deploy_key_administration import (
    DeployKeyAdministrationError,
    DeployKeyAdministrativeResult,
    apply_deploy_key_administrative_request,
)
from .deploy_key_promotion_authority import (
    DeployKeyPromotionAuthority,
    DeployKeyPromotionAuthorityError,
)
from .deploy_key_publication_authority import (
    DeployKeyPublicationAuthority,
    DeployKeyPublicationAuthorityError,
)
from .deploy_key_retention_authority import (
    DeployKeyRetentionAuthority,
    DeployKeyRetentionAuthorityError,
)
from .deploy_key_rollback_authority import (
    DeployKeyRollbackRepositoryAuthority,
    DeployKeyRollbackRepositoryAuthorityError,
)
from .github_repo import RepositoryVerificationError, fetch_and_verify_private_repository
from .local_change_service import LocalChangeService, LocalChangeServiceError
from .local_startup import LocalStartupError, LocalStartupResult, run_startup_local_sync
from .log_sync_service import LogSyncService, LogSyncServiceError
from .retrigger_cycle import RetriggerCycleError, run_retrigger_cycle
from .retrigger_ipc import (
    RetriggerIPCError,
    RetriggerRequest,
    RetriggerServer,
    request_retrigger_once,
    retrigger_socket_path,
)
from .runtime_event_bridge import RuntimeEventBridge, RuntimeEventBridgeError
from .runtime_startup import RuntimeStartupError, RuntimeStartupResult, run_startup_runtime_sync
from .state import AlreadyRunning, StateError, StateStore

_LOCAL_CHANGE_QUIET_SECONDS = 1.0
_DATABASE_SYNC_INTERVAL_SECONDS = 60.0 * 60.0
_LOG_SYNC_INTERVAL_SECONDS = 60.0 * 60.0
_CANDIDATE_DETECTION_INTERVAL_SECONDS = 60.0


def emit(event: str, *, level: str = "info", **fields: object) -> None:
    """Emit explicitly selected fields, never exception text or raw options."""
    print(
        json.dumps(
            {"timestamp": datetime.now(UTC).isoformat(), "level": level, "event": event, **fields},
            separators=(",", ":"),
        ),
        flush=True,
    )


class Shutdown:
    """A signal-safe flag; handlers must not acquire threading locks."""

    def __init__(self) -> None:
        self.requested = False

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.25))
        return self.requested


def _ensure_private_work_directory(protected: Path, directory: Path) -> None:
    """Create and verify an app-owned private directory without following symlinks."""
    if directory == protected or protected not in directory.parents:
        raise RetriggerCycleError("Retrigger work directory escapes protected storage")
    current = protected
    for part in directory.relative_to(protected).parts:
        current = current / part
        with suppress(FileExistsError):
            current.mkdir(mode=0o700)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(current, flags)
        except OSError as exc:
            raise RetriggerCycleError("Retrigger work directory is unsafe") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise RetriggerCycleError("Retrigger work directory is unsafe")
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)


def _local_work_roots(
    data_dir: Path,
    home_assistant_root: Path,
) -> tuple[Path, Path]:
    """Return verified app-owned roots for Local snapshots and Git metadata."""
    protected = (data_dir / "syncapp").resolve(strict=True)
    home = home_assistant_root.resolve(strict=True)
    if protected == home or protected in home.parents or home in protected.parents:
        raise RetriggerCycleError("Local work root overlaps Home Assistant source")
    work = protected / "work"
    roots = (work / "main-snapshots", work / "main-workspaces")
    for root in roots:
        _ensure_private_work_directory(protected, root)
    return roots


def _database_work_roots(
    data_dir: Path,
    home_assistant_root: Path,
) -> tuple[Path, Path, Path]:
    """Return verified app-owned roots for Recorder staging, snapshots and Git metadata."""
    protected = (data_dir / "syncapp").resolve(strict=True)
    home = home_assistant_root.resolve(strict=True)
    if protected == home or protected in home.parents or home in protected.parents:
        raise RetriggerCycleError("database work root overlaps Home Assistant source")
    work = protected / "work"
    roots = (
        work / "database-staging",
        work / "database-snapshots",
        work / "database-workspaces",
    )
    for root in roots:
        _ensure_private_work_directory(protected, root)
    return roots


def _runtime_work_roots(data_dir: Path) -> tuple[Path, Path, Path]:
    """Return verified app-owned roots for runtime artifacts, snapshots and Git metadata."""
    protected = (data_dir / "syncapp").resolve(strict=True)
    work = protected / "work"
    roots = (
        work / "runtime-staging",
        work / "runtime-snapshots",
        work / "runtime-workspaces",
    )
    for root in roots:
        _ensure_private_work_directory(protected, root)
    return roots


def _log_work_roots(data_dir: Path) -> tuple[Path, Path, Path]:
    """Return verified app-owned roots for immutable log artifacts and Git metadata."""
    protected = (data_dir / "syncapp").resolve(strict=True)
    work = protected / "work"
    roots = (
        work / "log-artifacts",
        work / "log-snapshots",
        work / "log-workspaces",
    )
    for root in roots:
        _ensure_private_work_directory(protected, root)
    return roots


def _work_roots(data_dir: Path, home_assistant_root: Path) -> tuple[Path, ...]:
    protected = (data_dir / "syncapp").resolve(strict=True)
    home = home_assistant_root.resolve(strict=True)
    work = protected / "work"
    if protected == home or protected in home.parents or home in protected.parents:
        raise RetriggerCycleError("Retrigger protected work root overlaps Home Assistant source")
    log_artifact_root, log_snapshot_root, log_workspace_root = _log_work_roots(data_dir)
    return (
        work / "main-snapshots",
        work / "main-workspaces",
        work / "database-staging",
        work / "database-snapshots",
        work / "database-workspaces",
        work / "runtime-staging",
        work / "runtime-snapshots",
        work / "runtime-workspaces",
        log_artifact_root,
        log_snapshot_root,
        log_workspace_root,
    )


def _run_startup_local_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    home_assistant_root: Path = Path("/homeassistant"),
    publication_authority: DeployKeyPublicationAuthority | None = None,
) -> LocalStartupResult | None:
    """Bootstrap one normal Local generation only for a trusted configured Repo B."""
    if config.repo_b is None or config.github_token is None:
        return None
    if store.repository_id(config.repo_b) is None:
        raise LocalStartupError("startup Local repository is not trusted")
    try:
        snapshot_root, workspace_root = _local_work_roots(data_dir, home_assistant_root)
        return run_startup_local_sync(
            store,
            home_assistant_root,
            snapshot_root,
            workspace_root,
            config.repo_b,
            publication_authority or config.github_token,
            recorder_database=(
                None
                if config.recorder_database_path is None
                else Path(config.recorder_database_path)
            ),
        )
    except (RetriggerCycleError, OSError) as exc:
        raise LocalStartupError("startup Local synchronization failed closed") from exc


def _local_change_service_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    home_assistant_root: Path = Path("/homeassistant"),
    publication_authority: DeployKeyPublicationAuthority | None = None,
) -> LocalChangeService | None:
    """Build, but do not start, event-driven Local synchronization after bootstrap."""
    if config.repo_b is None or config.github_token is None:
        return None
    if store.repository_id(config.repo_b) is None:
        raise LocalChangeServiceError("local change service repository is not trusted")
    try:
        snapshot_root, workspace_root = _local_work_roots(data_dir, home_assistant_root)
    except (RetriggerCycleError, OSError) as exc:
        raise LocalChangeServiceError("local change service work roots are unavailable") from exc
    return LocalChangeService(
        store,
        home_assistant_root,
        snapshot_root,
        workspace_root,
        config.repo_b,
        publication_authority or config.github_token,
        quiet_seconds=_LOCAL_CHANGE_QUIET_SECONDS,
        recorder_database=(
            None if config.recorder_database_path is None else Path(config.recorder_database_path)
        ),
    )


def _run_startup_database_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    home_assistant_root: Path = Path("/homeassistant"),
    publication_authority: DeployKeyPublicationAuthority | None = None,
) -> DatabaseStartupResult | None:
    """Bootstrap one normal Recorder generation only after trust and explicit selection."""
    if (
        config.repo_b is None
        or config.github_token is None
        or config.recorder_database_path is None
    ):
        return None
    if store.repository_id(config.repo_b) is None:
        raise DatabaseStartupError("startup database repository is not trusted")

    source_database = Path(config.recorder_database_path)
    try:
        canonical_home = home_assistant_root.resolve(strict=True)
        canonical_source = source_database.resolve(strict=True)
        if not canonical_home.is_dir():
            raise DatabaseStartupError("startup Home Assistant source is invalid")
        if canonical_source == canonical_home or canonical_home not in canonical_source.parents:
            raise DatabaseStartupError("startup database source escapes Home Assistant source")
        database_staging_root, snapshot_staging_root, workspace_root = _database_work_roots(
            data_dir, home_assistant_root
        )
        return run_startup_database_sync(
            store,
            source_database,
            database_staging_root,
            snapshot_staging_root,
            workspace_root,
            config.repo_b,
            publication_authority or config.github_token,
        )
    except (RetriggerCycleError, OSError) as exc:
        raise DatabaseStartupError("startup database synchronization failed closed") from exc


def _database_sync_service_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    home_assistant_root: Path = Path("/homeassistant"),
    publication_authority: DeployKeyPublicationAuthority | None = None,
    retention_authority: DeployKeyRetentionAuthority | None = None,
) -> DatabaseSyncService | None:
    """Build periodic Recorder processing only after the trusted startup generation."""
    if (
        config.repo_b is None
        or config.github_token is None
        or config.recorder_database_path is None
    ):
        return None
    if store.repository_id(config.repo_b) is None:
        raise DatabaseSyncServiceError("database service repository is not trusted")

    source_database = Path(config.recorder_database_path)
    try:
        canonical_home = home_assistant_root.resolve(strict=True)
        canonical_source = source_database.resolve(strict=True)
        if not canonical_home.is_dir():
            raise DatabaseSyncServiceError("database service Home Assistant source is invalid")
        if canonical_source == canonical_home or canonical_home not in canonical_source.parents:
            raise DatabaseSyncServiceError("database service source escapes Home Assistant source")
        database_staging_root, snapshot_staging_root, workspace_root = _database_work_roots(
            data_dir, home_assistant_root
        )
    except (RetriggerCycleError, OSError) as exc:
        raise DatabaseSyncServiceError("database service work roots are unavailable") from exc

    return DatabaseSyncService(
        store,
        source_database,
        database_staging_root,
        snapshot_staging_root,
        workspace_root,
        config.repo_b,
        config.github_token,
        interval_seconds=_DATABASE_SYNC_INTERVAL_SECONDS,
        retention_days=config.recorder_retention_days,
        publication_credential=publication_authority,
        retention_authority=retention_authority,
    )


def _run_startup_runtime_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    publication_authority: DeployKeyPublicationAuthority | None = None,
) -> RuntimeStartupResult | None:
    """Bootstrap one normal runtime generation only for a trusted configured Repo B."""
    if config.repo_b is None or config.github_token is None:
        return None
    if store.repository_id(config.repo_b) is None:
        raise RuntimeStartupError("startup runtime repository is not trusted")
    runtime_staging_root, runtime_snapshot_root, runtime_workspace_root = _runtime_work_roots(
        data_dir
    )
    return run_startup_runtime_sync(
        store,
        runtime_staging_root,
        runtime_snapshot_root,
        runtime_workspace_root,
        config.repo_b,
        publication_authority or config.github_token,
        core_token=os.environ.get("SUPERVISOR_TOKEN"),
    )


def _runtime_event_bridge_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    publication_authority: DeployKeyPublicationAuthority | None = None,
) -> RuntimeEventBridge | None:
    """Build, but do not start, normal runtime event processing after trust/bootstrap."""
    if config.repo_b is None or config.github_token is None:
        return None
    if store.repository_id(config.repo_b) is None:
        raise RuntimeEventBridgeError("runtime event bridge repository is not trusted")
    runtime_staging_root, runtime_snapshot_root, runtime_workspace_root = _runtime_work_roots(
        data_dir
    )
    return RuntimeEventBridge(
        store,
        runtime_staging_root,
        runtime_snapshot_root,
        runtime_workspace_root,
        config.repo_b,
        publication_authority or config.github_token,
        core_token=os.environ.get("SUPERVISOR_TOKEN"),
    )


def _log_sync_service_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    publication_authority: DeployKeyPublicationAuthority | None = None,
    retention_authority: DeployKeyRetentionAuthority | None = None,
) -> LogSyncService | None:
    """Build periodic log collection only for a trusted configured Repo B."""
    if config.repo_b is None or config.github_token is None:
        return None
    if store.repository_id(config.repo_b) is None:
        raise LogSyncServiceError("logs service repository is not trusted")
    try:
        artifact_root, snapshot_root, workspace_root = _log_work_roots(data_dir)
    except (RetriggerCycleError, OSError) as exc:
        raise LogSyncServiceError("logs service work roots are unavailable") from exc
    return LogSyncService(
        store,
        artifact_root,
        snapshot_root,
        workspace_root,
        config.repo_b,
        config.github_token,
        core_token=os.environ.get("SUPERVISOR_TOKEN"),
        interval_seconds=_LOG_SYNC_INTERVAL_SECONDS,
        publication_credential=publication_authority,
        retention_authority=retention_authority,
    )


def _candidate_detection_service_if_configured(
    store: StateStore,
    config: Config,
    deploy_key_ingress: DeployKeyCandidateIngress | None = None,
) -> CandidateDetectionService | None:
    """Build periodic trusted candidate intake for a configured Repo B."""
    if config.repo_b is None or config.github_token is None:
        return None
    if store.repository_id(config.repo_b) is None:
        raise CandidateDetectionServiceError("candidate service repository is not trusted")
    return CandidateDetectionService(
        store,
        config.repo_b,
        None if deploy_key_ingress is not None else config.github_token,
        deploy_key_ingress=deploy_key_ingress,
        interval_seconds=_CANDIDATE_DETECTION_INTERVAL_SECONDS,
    )


def _candidate_deploy_key_ingress_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
) -> DeployKeyCandidateIngress | None:
    """Build explicit candidate-only SSH authority after durable initialization."""
    if config.repo_b_candidate_transport == "token":
        return None
    if config.repo_b is None or config.github_token is None:
        raise CandidateDetectionServiceError("candidate deploy-key configuration is invalid")
    repository_id = store.repository_id(config.repo_b)
    if repository_id is None:
        raise CandidateDetectionServiceError("candidate service repository is not trusted")
    if store.synchronization_baseline(config.repo_b, "main") is None:
        raise CandidateDetectionServiceError("candidate repository is not initialized")
    try:
        protected = (data_dir / "syncapp").resolve(strict=True)
        key_directory = protected / "repo-b-deploy-key"
        access_work_directory = protected / "work" / "deploy-key-access"
        _ensure_private_work_directory(protected, access_work_directory)
        proof = test_repo_b_deploy_key_access(
            config.repo_b,
            config.github_token,
            repository_id,
            key_directory,
            work_directory=access_work_directory,
        )
        return DeployKeyCandidateIngress(proof, key_directory, access_work_directory)
    except (
        CandidateDeployKeyIngressError,
        DeployKeyAccessError,
        RetriggerCycleError,
        OSError,
    ) as exc:
        raise CandidateDetectionServiceError(
            "candidate deploy-key authority is unavailable"
        ) from exc


def _deploy_key_publication_authority_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
) -> DeployKeyPublicationAuthority | None:
    """Build ordinary non-force SSH publication authority after initialization."""
    if config.repo_b_publication_transport == "token":
        return None
    if config.repo_b is None or config.github_token is None:
        raise LocalStartupError("publication deploy-key configuration is invalid")
    repository_id = store.repository_id(config.repo_b)
    if repository_id is None:
        raise LocalStartupError("publication repository is not trusted")
    if store.synchronization_baseline(config.repo_b, "main") is None:
        raise LocalStartupError("publication repository is not initialized")
    try:
        protected = (data_dir / "syncapp").resolve(strict=True)
        key_directory = protected / "repo-b-deploy-key"
        access_work_directory = protected / "work" / "deploy-key-publication-access"
        _ensure_private_work_directory(protected, access_work_directory)
        proof = test_repo_b_deploy_key_access(
            config.repo_b,
            config.github_token,
            repository_id,
            key_directory,
            work_directory=access_work_directory,
        )
        return DeployKeyPublicationAuthority(
            proof,
            key_directory,
            access_work_directory,
        )
    except (
        DeployKeyAccessError,
        DeployKeyPublicationAuthorityError,
        RetriggerCycleError,
        OSError,
    ) as exc:
        raise LocalStartupError("publication deploy-key authority is unavailable") from exc


def _deploy_key_promotion_authority_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    home_assistant_root: Path,
) -> DeployKeyPromotionAuthority | None:
    """Build candidate promotion SSH authority after durable initialization."""
    if config.repo_b_promotion_transport == "token":
        return None
    if config.repo_b is None or config.github_token is None:
        raise RetriggerCycleError("promotion deploy-key configuration is invalid")
    repository_id = store.repository_id(config.repo_b)
    if repository_id is None:
        raise RetriggerCycleError("promotion repository is not trusted")
    if store.synchronization_baseline(config.repo_b, "main") is None:
        raise RetriggerCycleError("promotion repository is not initialized")
    try:
        protected = (data_dir / "syncapp").resolve(strict=True)
        home = home_assistant_root.resolve(strict=True)
        key_directory = protected / "repo-b-deploy-key"
        access_work_directory = protected / "work" / "deploy-key-promotion-access"
        workspace_root = protected / "work" / "deploy-key-promotion"
        _ensure_private_work_directory(protected, access_work_directory)
        _ensure_private_work_directory(protected, workspace_root)
        proof = test_repo_b_deploy_key_access(
            config.repo_b,
            config.github_token,
            repository_id,
            key_directory,
            work_directory=access_work_directory,
        )
        return DeployKeyPromotionAuthority(
            proof, key_directory, access_work_directory, workspace_root, home
        )
    except (
        DeployKeyAccessError,
        DeployKeyPromotionAuthorityError,
        RetriggerCycleError,
        OSError,
    ) as exc:
        raise RetriggerCycleError("promotion deploy-key authority is unavailable") from exc


def _deploy_key_rollback_authority_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
) -> DeployKeyRollbackRepositoryAuthority | None:
    """Build read-only rollback repository authority after durable initialization."""
    if config.repo_b_rollback_transport == "token":
        return None
    if config.repo_b is None or config.github_token is None:
        raise RetriggerCycleError("rollback deploy-key configuration is invalid")
    repository_id = store.repository_id(config.repo_b)
    if repository_id is None:
        raise RetriggerCycleError("rollback repository is not trusted")
    if store.synchronization_baseline(config.repo_b, "main") is None:
        raise RetriggerCycleError("rollback repository is not initialized")
    try:
        protected = (data_dir / "syncapp").resolve(strict=True)
        key_directory = protected / "repo-b-deploy-key"
        access_work_directory = protected / "work" / "deploy-key-rollback-access"
        _ensure_private_work_directory(protected, access_work_directory)
        proof = test_repo_b_deploy_key_access(
            config.repo_b,
            config.github_token,
            repository_id,
            key_directory,
            work_directory=access_work_directory,
        )
        return DeployKeyRollbackRepositoryAuthority(
            proof,
            key_directory,
            access_work_directory,
        )
    except (
        DeployKeyAccessError,
        DeployKeyRollbackRepositoryAuthorityError,
        RetriggerCycleError,
        OSError,
    ) as exc:
        raise RetriggerCycleError("rollback deploy-key authority is unavailable") from exc


def _deploy_key_retention_authority_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
) -> DeployKeyRetentionAuthority | None:
    """Build generated-history SSH authority after durable initialization."""

    if config.repo_b_retention_transport == "token":
        return None
    if config.repo_b is None or config.github_token is None:
        raise RetriggerCycleError("retention deploy-key configuration is invalid")
    repository_id = store.repository_id(config.repo_b)
    if repository_id is None:
        raise RetriggerCycleError("retention repository is not trusted")
    if store.synchronization_baseline(config.repo_b, "main") is None:
        raise RetriggerCycleError("retention repository is not initialized")
    try:
        protected = (data_dir / "syncapp").resolve(strict=True)
        key_directory = protected / "repo-b-deploy-key"
        access_work_directory = protected / "work" / "deploy-key-retention-access"
        staging_root = protected / "work" / "deploy-key-retention"
        _ensure_private_work_directory(protected, access_work_directory)
        _ensure_private_work_directory(protected, staging_root)
        proof = test_repo_b_deploy_key_access(
            config.repo_b,
            config.github_token,
            repository_id,
            key_directory,
            work_directory=access_work_directory,
        )
        return DeployKeyRetentionAuthority(
            proof,
            key_directory,
            access_work_directory,
            staging_root,
        )
    except (
        DeployKeyAccessError,
        DeployKeyRetentionAuthorityError,
        RetriggerCycleError,
        OSError,
    ) as exc:
        raise RetriggerCycleError("retention deploy-key authority is unavailable") from exc


def _run_deploy_key_administration_if_configured(
    store: StateStore,
    config: Config,
    data_dir: Path,
    repository_id: int,
    home_assistant_root: Path = Path("/homeassistant"),
) -> DeployKeyAdministrativeResult | None:
    """Apply one explicit lifecycle action after Repo B identity is pinned."""

    request = config.deploy_key_administrative_request
    if request is None:
        return None
    if config.repo_b is None or config.github_token is None:
        raise DeployKeyAdministrationError("deploy-key administration is not configured")
    try:
        protected = (data_dir / "syncapp").resolve(strict=True)
        work = protected / "work"
        access = work / "deploy-key-administration-access"
        snapshots = work / "repo-b-initialization-snapshots"
        workspaces = work / "repo-b-initialization-workspaces"
        for directory in (access, snapshots, workspaces):
            _ensure_private_work_directory(protected, directory)
        result = apply_deploy_key_administrative_request(
            store,
            request,
            target=config.repo_b,
            repository_id=repository_id,
            github_token=config.github_token,
            key_directory=protected / "repo-b-deploy-key",
            work_directory=access,
            source=home_assistant_root,
            snapshot_root=snapshots,
            workspace_root=workspaces,
        )
    except (RetriggerCycleError, OSError) as exc:
        raise DeployKeyAdministrationError(
            "deploy-key administrative work roots are unavailable"
        ) from exc

    if result.replayed:
        emit(
            "repo_b_admin_skipped",
            action=request.action,
            outcome=result.outcome,
        )
    elif result.outcome in {"blocked", "retry"}:
        emit(
            "repo_b_admin_deferred",
            level="warning",
            action=request.action,
            outcome=result.outcome,
        )
    elif result.public_key is not None:
        emit(
            "repo_b_admin_completed",
            action=request.action,
            outcome=result.outcome,
            public_key=result.public_key,
            fingerprint=result.fingerprint,
            generation_id=result.generation_id,
        )
    elif result.fingerprint is not None:
        emit(
            "repo_b_admin_completed",
            action=request.action,
            outcome=result.outcome,
            fingerprint=result.fingerprint,
            generation_id=result.generation_id,
        )
    else:
        emit(
            "repo_b_admin_completed",
            action=request.action,
            outcome=result.outcome,
        )
    return result


def _handle_retrigger_request(
    store: StateStore,
    config: Config,
    data_dir: Path,
    request: RetriggerRequest,
    candidate_deploy_key_ingress: DeployKeyCandidateIngress | None = None,
    deploy_key_publication_authority: DeployKeyPublicationAuthority | None = None,
    deploy_key_promotion_authority: DeployKeyPromotionAuthority | None = None,
    deploy_key_rollback_authority: DeployKeyRollbackRepositoryAuthority | None = None,
    deploy_key_retention_authority: DeployKeyRetentionAuthority | None = None,
) -> str:
    """Execute one bounded outbound cycle while the service retains state ownership."""
    if config.repo_b is None or config.github_token is None:
        return "configuration_invalid"
    try:
        expected_id = store.repository_id(config.repo_b)
        if expected_id is None:
            return "repo_b_untrusted"
        identity = fetch_and_verify_private_repository(
            config.repo_b,
            config.github_token,
            expected_id=expected_id,
        )
        store.bind_repository(config.repo_b, identity.repository_id)
        (
            snapshot_staging_root,
            local_workspace_root,
            database_staging_root,
            database_snapshot_root,
            database_workspace_root,
            runtime_staging_root,
            runtime_snapshot_root,
            runtime_workspace_root,
            log_artifact_root,
            log_snapshot_root,
            log_workspace_root,
        ) = _work_roots(data_dir, request.home_assistant_root)
        run_retrigger_cycle(
            store,
            request.home_assistant_root,
            snapshot_staging_root,
            local_workspace_root,
            request.recorder_database,
            database_staging_root,
            database_snapshot_root,
            database_workspace_root,
            runtime_staging_root,
            runtime_snapshot_root,
            runtime_workspace_root,
            config.repo_b,
            config.github_token,
            candidate_deploy_key_ingress=candidate_deploy_key_ingress,
            deploy_key_publication_authority=deploy_key_publication_authority,
            deploy_key_promotion_authority=deploy_key_promotion_authority,
            deploy_key_rollback_authority=deploy_key_rollback_authority,
            deploy_key_retention_authority=deploy_key_retention_authority,
            core_token=os.environ.get("SUPERVISOR_TOKEN"),
            log_artifact_root=log_artifact_root,
            log_snapshot_root=log_snapshot_root,
            log_workspace_root=log_workspace_root,
            recorder_retention_days=config.recorder_retention_days,
            deployment_observation_seconds=config.deployment_observation_seconds,
        )
    except RepositoryVerificationError:
        return "repo_b_untrusted"
    except (RetriggerCycleError, OSError):
        return "cycle_failed"
    except StateError:
        return "internal_error"
    return "completed"


def run(data_dir: Path, stop: Shutdown) -> None:
    config = load_config(data_dir / "options.json")
    with StateStore(data_dir) as store:
        if config.administrative_retry_request is not None:
            retry_result = apply_administrative_retry_request(
                store,
                config.administrative_retry_request,
            )
            if retry_result.replayed:
                emit("administrative_retry_skipped")
            elif retry_result.outcome == "retried":
                emit("administrative_retry_completed")
            else:
                emit("administrative_retry_rejected", level="warning")
        candidate_deploy_key_ingress: DeployKeyCandidateIngress | None = None
        deploy_key_publication_authority: DeployKeyPublicationAuthority | None = None
        deploy_key_promotion_authority: DeployKeyPromotionAuthority | None = None
        deploy_key_rollback_authority: DeployKeyRollbackRepositoryAuthority | None = None
        deploy_key_retention_authority: DeployKeyRetentionAuthority | None = None
        if config.repo_b is not None and config.github_token is not None:
            expected_id = store.repository_id(config.repo_b)
            identity = fetch_and_verify_private_repository(
                config.repo_b,
                config.github_token,
                expected_id=expected_id,
            )
            store.bind_repository(config.repo_b, identity.repository_id)
            _run_deploy_key_administration_if_configured(
                store,
                config,
                data_dir,
                identity.repository_id,
            )
            candidate_deploy_key_ingress = _candidate_deploy_key_ingress_if_configured(
                store, config, data_dir
            )
            deploy_key_publication_authority = _deploy_key_publication_authority_if_configured(
                store, config, data_dir
            )
            deploy_key_promotion_authority = _deploy_key_promotion_authority_if_configured(
                store, config, data_dir, Path("/homeassistant")
            )
            deploy_key_rollback_authority = _deploy_key_rollback_authority_if_configured(
                store, config, data_dir
            )
            deploy_key_retention_authority = _deploy_key_retention_authority_if_configured(
                store, config, data_dir
            )
        boot = store.start_run()
        local_change_service: LocalChangeService | None = None
        database_sync_service: DatabaseSyncService | None = None
        runtime_bridge: RuntimeEventBridge | None = None
        log_sync_service: LogSyncService | None = None
        candidate_detection_service: CandidateDetectionService | None = None
        if not stop.requested:
            if deploy_key_publication_authority is None:
                _run_startup_local_if_configured(store, config, data_dir)
            else:
                _run_startup_local_if_configured(
                    store,
                    config,
                    data_dir,
                    publication_authority=deploy_key_publication_authority,
                )
            if not stop.requested:
                if deploy_key_publication_authority is None:
                    local_change_service = _local_change_service_if_configured(
                        store, config, data_dir
                    )
                else:
                    local_change_service = _local_change_service_if_configured(
                        store,
                        config,
                        data_dir,
                        publication_authority=deploy_key_publication_authority,
                    )
            if not stop.requested:
                if deploy_key_publication_authority is None:
                    _run_startup_database_if_configured(store, config, data_dir)
                else:
                    _run_startup_database_if_configured(
                        store,
                        config,
                        data_dir,
                        publication_authority=deploy_key_publication_authority,
                    )
            if not stop.requested:
                if (
                    deploy_key_publication_authority is None
                    and deploy_key_retention_authority is None
                ):
                    database_sync_service = _database_sync_service_if_configured(
                        store,
                        config,
                        data_dir,
                    )
                elif deploy_key_publication_authority is None:
                    database_sync_service = _database_sync_service_if_configured(
                        store,
                        config,
                        data_dir,
                        retention_authority=deploy_key_retention_authority,
                    )
                elif deploy_key_retention_authority is None:
                    database_sync_service = _database_sync_service_if_configured(
                        store,
                        config,
                        data_dir,
                        publication_authority=deploy_key_publication_authority,
                    )
                else:
                    database_sync_service = _database_sync_service_if_configured(
                        store,
                        config,
                        data_dir,
                        publication_authority=deploy_key_publication_authority,
                        retention_authority=deploy_key_retention_authority,
                    )
            if not stop.requested:
                if deploy_key_publication_authority is None:
                    _run_startup_runtime_if_configured(store, config, data_dir)
                else:
                    _run_startup_runtime_if_configured(
                        store,
                        config,
                        data_dir,
                        publication_authority=deploy_key_publication_authority,
                    )
            if not stop.requested:
                if deploy_key_publication_authority is None:
                    runtime_bridge = _runtime_event_bridge_if_configured(store, config, data_dir)
                else:
                    runtime_bridge = _runtime_event_bridge_if_configured(
                        store,
                        config,
                        data_dir,
                        publication_authority=deploy_key_publication_authority,
                    )
            if not stop.requested:
                if (
                    deploy_key_publication_authority is None
                    and deploy_key_retention_authority is None
                ):
                    log_sync_service = _log_sync_service_if_configured(
                        store,
                        config,
                        data_dir,
                    )
                elif deploy_key_publication_authority is None:
                    log_sync_service = _log_sync_service_if_configured(
                        store,
                        config,
                        data_dir,
                        retention_authority=deploy_key_retention_authority,
                    )
                elif deploy_key_retention_authority is None:
                    log_sync_service = _log_sync_service_if_configured(
                        store,
                        config,
                        data_dir,
                        publication_authority=deploy_key_publication_authority,
                    )
                else:
                    log_sync_service = _log_sync_service_if_configured(
                        store,
                        config,
                        data_dir,
                        publication_authority=deploy_key_publication_authority,
                        retention_authority=deploy_key_retention_authority,
                    )
            if not stop.requested:
                if candidate_deploy_key_ingress is None:
                    candidate_detection_service = _candidate_detection_service_if_configured(
                        store, config
                    )
                else:
                    candidate_detection_service = _candidate_detection_service_if_configured(
                        store, config, candidate_deploy_key_ingress
                    )
        socket_path = retrigger_socket_path(data_dir.resolve(strict=True))
        next_status = time.monotonic() + config.status_interval_seconds
        mode = (
            "active"
            if local_change_service is not None
            or database_sync_service is not None
            or runtime_bridge is not None
            or log_sync_service is not None
            or candidate_detection_service is not None
            else "passive"
        )
        local_change_started = False
        database_sync_started = False
        runtime_bridge_started = False
        log_sync_started = False
        candidate_detection_started = False

        try:
            if not stop.requested and local_change_service is not None:
                local_change_service.start(time.monotonic())
                local_change_started = True
            if not stop.requested and database_sync_service is not None:
                database_sync_service.start(time.monotonic())
                database_sync_started = True
            if not stop.requested and runtime_bridge is not None:
                runtime_bridge.start()
                runtime_bridge_started = True
            if not stop.requested and log_sync_service is not None:
                log_sync_service.start(time.monotonic())
                log_sync_started = True
            if not stop.requested and candidate_detection_service is not None:
                candidate_detection_service.start(time.monotonic())
                candidate_detection_started = True
            with RetriggerServer(socket_path) as retrigger_server:
                fields = {**asdict(boot), "version": __version__, "mode": mode}
                level = "warning" if boot.interrupted_run_id else "info"
                if config.log_level == "info" or (
                    config.log_level == "warning" and level == "warning"
                ):
                    emit("service_started", level=level, **fields)

                while not stop.requested:
                    try:
                        retrigger_server.serve_once(
                            lambda request: _handle_retrigger_request(
                                store,
                                config,
                                data_dir,
                                request,
                                candidate_deploy_key_ingress,
                                deploy_key_publication_authority,
                                deploy_key_promotion_authority,
                                deploy_key_rollback_authority,
                                deploy_key_retention_authority,
                            ),
                            timeout_seconds=0.25,
                        )
                    except RetriggerIPCError:
                        if stop.requested:
                            break
                        raise
                    if stop.requested:
                        break
                    now = time.monotonic()
                    if (
                        not stop.requested
                        and local_change_started
                        and local_change_service is not None
                    ):
                        local_change_service.tick(now)
                    if (
                        not stop.requested
                        and database_sync_started
                        and database_sync_service is not None
                    ):
                        database_sync_service.tick(now)
                    if not stop.requested and runtime_bridge_started and runtime_bridge is not None:
                        runtime_bridge.tick()
                    if not stop.requested and log_sync_started and log_sync_service is not None:
                        log_sync_service.tick(now)
                    if (
                        not stop.requested
                        and candidate_detection_started
                        and candidate_detection_service is not None
                    ):
                        candidate_detection_service.tick(now)
                    if not stop.requested and now >= next_status:
                        if config.log_level == "info":
                            emit("service_idle", run_id=boot.run_id, mode=mode)
                        next_status = now + config.status_interval_seconds
        except BaseException:
            if local_change_started and local_change_service is not None:
                with suppress(LocalChangeServiceError):
                    local_change_service.stop()
            if database_sync_started and database_sync_service is not None:
                with suppress(DatabaseSyncServiceError):
                    database_sync_service.stop()
            if runtime_bridge_started and runtime_bridge is not None:
                with suppress(RuntimeEventBridgeError):
                    runtime_bridge.stop()
            if log_sync_started and log_sync_service is not None:
                with suppress(LogSyncServiceError):
                    log_sync_service.stop()
            if candidate_detection_started and candidate_detection_service is not None:
                with suppress(CandidateDetectionServiceError):
                    candidate_detection_service.stop()
            raise

        if local_change_started and local_change_service is not None:
            local_change_service.stop()
        if database_sync_started and database_sync_service is not None:
            database_sync_service.stop()
        if runtime_bridge_started and runtime_bridge is not None:
            runtime_bridge.stop()
        if log_sync_started and log_sync_service is not None:
            log_sync_service.stop()
        if candidate_detection_started and candidate_detection_service is not None:
            candidate_detection_service.stop()
        store.finish_run()
        if config.log_level == "info":
            emit("service_stopped", run_id=boot.run_id)


def _run_retrigger_client(
    data_dir: Path,
    home_assistant_root: Path | None,
    recorder_database: Path | None,
) -> int:
    if home_assistant_root is None or recorder_database is None:
        emit("retrigger_failed", level="error", reason="source_paths_required")
        return 2
    try:
        socket_path = retrigger_socket_path(data_dir.resolve(strict=True))
        request_retrigger_once(socket_path, home_assistant_root, recorder_database)
    except (RetriggerIPCError, OSError):
        emit("retrigger_failed", level="error", reason="request_failed")
        return 6
    emit("retrigger_completed", mode="one_shot")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Home Assistant SyncApp foundation service")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--retrigger-once", action="store_true")
    parser.add_argument("--home-assistant-root", type=Path)
    parser.add_argument("--recorder-database", type=Path)
    args = parser.parse_args()

    if args.retrigger_once:
        return _run_retrigger_client(
            args.data_dir,
            args.home_assistant_root,
            args.recorder_database,
        )
    if args.home_assistant_root is not None or args.recorder_database is not None:
        emit("service_failed", level="error", reason="unexpected_source_paths")
        return 2

    stop = Shutdown()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop.requested = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)
    try:
        run(args.data_dir, stop)
    except ConfigError:
        emit("service_failed", level="error", reason="configuration_invalid")
        return 2
    except AlreadyRunning:
        emit("service_failed", level="error", reason="already_running")
        return 3
    except RepositoryVerificationError:
        emit("service_failed", level="error", reason="repo_b_untrusted")
        return 5
    except StateError:
        emit("service_failed", level="error", reason="state_unavailable")
        return 4
    except RetriggerIPCError:
        emit("service_failed", level="error", reason="retrigger_ipc_unavailable")
        return 6
    except RuntimeEventBridgeError:
        emit("service_failed", level="error", reason="runtime_events_unavailable")
        return 7
    except LocalStartupError:
        emit("service_failed", level="error", reason="local_sync_startup_failed")
        return 8
    except DatabaseStartupError:
        emit("service_failed", level="error", reason="database_sync_startup_failed")
        return 9
    except LocalChangeServiceError:
        emit("service_failed", level="error", reason="local_events_unavailable")
        return 10
    except DatabaseSyncServiceError:
        emit("service_failed", level="error", reason="database_sync_service_failed")
        return 11
    except LogSyncServiceError:
        emit("service_failed", level="error", reason="log_sync_service_failed")
        return 12
    except CandidateDetectionServiceError:
        emit("service_failed", level="error", reason="candidate_detection_service_failed")
        return 13
    except AdministrativeRetryRequestError:
        emit("service_failed", level="error", reason="administrative_retry_failed")
        return 14
    except DeployKeyAdministrationError:
        emit("service_failed", level="error", reason="deploy_key_administration_failed")
        return 15
    except Exception:
        emit("service_failed", level="error", reason="internal_error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
