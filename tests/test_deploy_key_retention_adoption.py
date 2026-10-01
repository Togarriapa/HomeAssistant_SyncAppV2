from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp import database_history_replace_transport as database_transport
from ha_syncapp import deploy_key_retention_authority as authority_module
from ha_syncapp import log_history_replace_transport as log_transport
from ha_syncapp.config import Config
from ha_syncapp.database_history_evidence import (
    DatabaseHistoryRecord,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_history_replacement import (
    DatabaseHistoryReplacementAuthorization,
)
from ha_syncapp.database_retention_work import discover_database_retention_work
from ha_syncapp.deploy_key_access import (
    DeployKeyAccessError,
    DeployKeyAccessProof,
    DeployKeyReference,
    DeployKeyReferenceSnapshot,
    DeployKeyTransportSession,
)
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_evidence import (
    LogHistoryRecord,
    validate_trusted_log_history_evidence,
)
from ha_syncapp.log_history_replacement import LogHistoryReplacementAuthorization
from ha_syncapp.log_retention_work import discover_log_retention_work
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
LOG_HEAD = "a" * 40
LOG_ROOT = "b" * 40
DATABASE_HEAD = "c" * 40
DATABASE_ROOT = "d" * 40
PROOF = DeployKeyAccessProof(
    target=TARGET,
    repository_id=REPOSITORY_ID,
    key_fingerprint="SHA256:" + "A" * 43,
    generation_id="123e4567-e89b-42d3-a456-426614174000",
    ref_count=2,
    observation_sha256="e" * 64,
)


def _authority(tmp_path: Path) -> authority_module.DeployKeyRetentionAuthority:
    return authority_module.DeployKeyRetentionAuthority(
        PROOF,
        tmp_path / "key",
        tmp_path / "access",
        tmp_path / "staging",
    )


def _snapshot(*references: DeployKeyReference) -> DeployKeyReferenceSnapshot:
    return DeployKeyReferenceSnapshot(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        key_fingerprint=PROOF.key_fingerprint,
        generation_id=PROOF.generation_id,
        references=tuple(references),
        observation_sha256="f" * 64,
    )


def _log_evidence():
    return validate_trusted_log_history_evidence(
        branch_head=BranchHead(TARGET, REPOSITORY_ID, "logs", LOG_HEAD),
        records=(
            LogHistoryRecord(LOG_HEAD, NOW - timedelta(days=1), (LOG_ROOT,)),
            LogHistoryRecord(LOG_ROOT, NOW - timedelta(days=40), ()),
        ),
        reference_time=NOW,
    )


def _database_evidence():
    return validate_trusted_database_history_evidence(
        branch_head=BranchHead(TARGET, REPOSITORY_ID, "database", DATABASE_HEAD),
        records=(
            DatabaseHistoryRecord(DATABASE_HEAD, NOW - timedelta(days=1), (DATABASE_ROOT,)),
            DatabaseHistoryRecord(DATABASE_ROOT, NOW - timedelta(days=10), ()),
        ),
        reference_time=NOW,
        retention_days=7,
    )


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, REPOSITORY_ID)
    return store


@pytest.mark.parametrize(
    ("branch", "expected"),
    [("logs", LOG_HEAD), ("database", DATABASE_HEAD)],
)
def test_authority_observes_exact_generated_branch_from_proof_bound_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    expected: str,
) -> None:
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: _snapshot(
            DeployKeyReference("refs/heads/database", DATABASE_HEAD),
            DeployKeyReference("refs/heads/logs", LOG_HEAD),
        ),
    )

    observed = _authority(tmp_path).observe(TARGET, REPOSITORY_ID, branch)

    assert observed == BranchHead(TARGET, REPOSITORY_ID, branch, expected)


def test_authority_rejects_duplicate_branch_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: _snapshot(
            DeployKeyReference("refs/heads/logs", LOG_HEAD),
            DeployKeyReference("refs/heads/logs", LOG_ROOT),
        ),
    )

    with pytest.raises(authority_module.DeployKeyRetentionAuthorityError):
        _authority(tmp_path).observe(TARGET, REPOSITORY_ID, "logs")


def test_bounded_history_parser_rejects_oversized_and_non_ascii_metadata() -> None:
    one = f"1 {LOG_HEAD}\n".encode()
    with pytest.raises(authority_module.DeployKeyRetentionAuthorityError, match="limit"):
        authority_module._parse_history(one + one, LOG_HEAD, 1)
    with pytest.raises(authority_module.DeployKeyRetentionAuthorityError, match="metadata"):
        authority_module._parse_history(b"\xff", LOG_HEAD, 2)


def test_log_history_reader_rejects_merge_history_and_removes_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "acquired"
    repository.mkdir()
    records = (
        authority_module._HistoryRecord(
            LOG_HEAD,
            NOW - timedelta(days=1),
            (LOG_ROOT, "9" * 40),
        ),
        authority_module._HistoryRecord(LOG_ROOT, NOW - timedelta(days=40), ()),
    )
    monkeypatch.setattr(
        authority_module.DeployKeyRetentionAuthority,
        "_acquire",
        lambda _self, _branch: (
            BranchHead(TARGET, REPOSITORY_ID, "logs", LOG_HEAD),
            records,
            repository,
        ),
    )

    with pytest.raises(authority_module.DeployKeyRetentionAuthorityError, match="invalid"):
        _authority(tmp_path).read_log_history(NOW)

    assert not repository.exists()


def test_history_acquisition_fetches_only_exact_branch_without_shallowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []
    descriptor_file = tmp_path / "descriptor-acquire"
    descriptor_file.write_text("not-a-private-key")
    descriptor = descriptor_file.open("rb")

    @contextmanager
    def session(*_args: object, **_kwargs: object) -> Iterator[DeployKeyTransportSession]:
        yield DeployKeyTransportSession(
            target=TARGET,
            repository_id=REPOSITORY_ID,
            key_fingerprint=PROOF.key_fingerprint,
            generation_id=PROOF.generation_id,
            git_executable=Path("/usr/bin/git"),
            work_directory=tmp_path,
            ssh_command="/usr/bin/ssh -o IdentitiesOnly=yes -i /dev/fd/7",
            environment={"GIT_TERMINAL_PROMPT": "0"},
            private_descriptor=descriptor.fileno(),
        )

    def local(
        _self: authority_module.DeployKeyRetentionAuthority,
        _repository: Path,
        arguments: tuple[str, ...],
    ) -> bytes:
        if arguments[0] == "init":
            return b""
        if arguments[0] == "rev-parse":
            return f"{LOG_HEAD}\n".encode()
        assert arguments[0] == "rev-list"
        return (
            f"{int((NOW - timedelta(days=1)).timestamp())} {LOG_HEAD} {LOG_ROOT}\n"
            f"{int((NOW - timedelta(days=40)).timestamp())} {LOG_ROOT}\n"
        ).encode()

    monkeypatch.setattr(
        authority_module.DeployKeyRetentionAuthority,
        "observe",
        lambda _self, _target, _repository_id, branch: BranchHead(
            TARGET, REPOSITORY_ID, branch, LOG_HEAD
        ),
    )
    monkeypatch.setattr(authority_module.DeployKeyRetentionAuthority, "_run_local", local)
    monkeypatch.setattr(authority_module, "open_repo_b_deploy_key_transport", session)
    monkeypatch.setattr(
        authority_module,
        "run_bounded_repo_b_git",
        lambda command, **_kwargs: commands.append(command) or b"",
    )
    try:
        _head, records, repository = _authority(tmp_path)._acquire("logs")
    finally:
        descriptor.close()

    try:
        assert tuple(record.sha for record in records) == (LOG_HEAD, LOG_ROOT)
        assert len(commands) == 1
        command = commands[0]
        assert command[-2:] == (
            "ssh://git@github.com/Owner/Private-Home.git",
            "+refs/heads/logs:refs/syncapp/retention",
        )
        assert "--no-tags" in command
        assert not any(argument.startswith("--depth") for argument in command)
        assert "token" not in " ".join(command).casefold()
    finally:
        repository.rmdir()


@pytest.mark.parametrize("transient", [False, True])
def test_authority_sanitizes_access_failure_and_preserves_retry_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient: bool
) -> None:
    monkeypatch.setattr(
        authority_module,
        "read_repo_b_deploy_key_references",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DeployKeyAccessError("private-key secret detail", transient=transient)
        ),
    )

    with pytest.raises(authority_module.DeployKeyRetentionAuthorityError) as caught:
        _authority(tmp_path).observe(TARGET, REPOSITORY_ID, "logs")

    assert caught.value.transient is transient
    assert "secret" not in str(caught.value)


def test_logs_discovery_uses_deploy_key_authority_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    authority = _authority(tmp_path)
    calls: list[datetime] = []
    monkeypatch.setattr(
        authority_module.DeployKeyRetentionAuthority,
        "read_log_history",
        lambda _self, reference_time: calls.append(reference_time) or _log_evidence(),
    )
    try:
        item = discover_log_retention_work(
            store,
            TARGET,
            None,
            reference_time=NOW,
            retention_authority=authority,
        )
    finally:
        store.__exit__(None, None, None)

    assert item.work_kind == "logs_retention"
    assert calls == [NOW]


def test_database_discovery_uses_deploy_key_authority_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    authority = _authority(tmp_path)
    calls: list[tuple[datetime, int]] = []
    monkeypatch.setattr(
        authority_module.DeployKeyRetentionAuthority,
        "read_database_history",
        lambda _self, reference_time, retention_days: (
            calls.append((reference_time, retention_days)) or _database_evidence()
        ),
    )
    try:
        item = discover_database_retention_work(
            store,
            TARGET,
            None,
            retention_days=7,
            reference_time=NOW,
            retention_authority=authority,
        )
    finally:
        store.__exit__(None, None, None)

    assert item.work_kind == "database_retention"
    assert calls == [(NOW, 7)]


def test_discovery_rejects_mixed_token_and_deploy_key_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        with pytest.raises(ValueError, match="exactly one"):
            discover_log_retention_work(
                store,
                TARGET,
                "token-secret",
                reference_time=NOW,
                retention_authority=_authority(tmp_path),
            )
    finally:
        store.__exit__(None, None, None)


def test_service_builds_retention_authority_only_after_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    protected = data / "syncapp"
    (protected / "repo-b-deploy-key").mkdir(parents=True, mode=0o700)
    config = Config(
        repo_b=TARGET,
        github_token="rest-identity-token",
        repo_b_retention_transport="deploy_key",
    )
    monkeypatch.setattr(service, "test_repo_b_deploy_key_access", lambda *_a, **_k: PROOF)
    with StateStore(data) as store:
        store.bind_repository(TARGET, REPOSITORY_ID)
        with pytest.raises(service.RetriggerCycleError, match="initialized"):
            service._deploy_key_retention_authority_if_configured(store, config, data)
        store.record_synchronization_baseline(
            TARGET, "main", "1" * 64, "2" * 40, synchronized_at=NOW
        )
        authority = service._deploy_key_retention_authority_if_configured(store, config, data)

    assert authority is not None
    assert "rest-identity-token" not in repr(authority)
    assert str(tmp_path) not in repr(authority)


def _artifact(kind: type[Any], repository: Path, branch: str, replacement: str) -> Any:
    artifact = object.__new__(kind)
    values = {
        "repository": repository.resolve(),
        "target": TARGET,
        "repository_id": REPOSITORY_ID,
        "branch": branch,
        "expected_head_sha": LOG_HEAD,
        "retained_shas": (LOG_HEAD,),
        "replacement_head_sha": replacement,
    }
    for name, value in values.items():
        object.__setattr__(artifact, name, value)
    return artifact


def _authorization(kind: type[Any], branch: str) -> Any:
    authorization = object.__new__(kind)
    values = {
        "target": TARGET,
        "repository_id": REPOSITORY_ID,
        "branch": branch,
        "expected_head_sha": LOG_HEAD,
        "retained_shas": (LOG_HEAD,),
        "pruned_shas": (LOG_ROOT,),
    }
    for name, value in values.items():
        object.__setattr__(authorization, name, value)
    return authorization


@pytest.mark.parametrize(
    ("transport", "branch", "authorization_type", "artifact_type", "replace_name"),
    [
        (
            log_transport,
            "logs",
            LogHistoryReplacementAuthorization,
            log_transport.LogHistoryReplacementArtifact,
            "replace_logs_history_with_deploy_key",
        ),
        (
            database_transport,
            "database",
            DatabaseHistoryReplacementAuthorization,
            database_transport.DatabaseHistoryReplacementArtifact,
            "replace_database_history_with_deploy_key",
        ),
    ],
)
def test_deploy_key_replacement_uses_descriptor_ssh_and_exact_force_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: Any,
    branch: str,
    authorization_type: type[Any],
    artifact_type: type[Any],
    replace_name: str,
) -> None:
    repository = tmp_path.resolve()
    (repository / ".git").mkdir()
    replacement = "8" * 40
    authorization = _authorization(authorization_type, branch)
    artifact = _artifact(artifact_type, repository, branch, replacement)
    commands: list[tuple[tuple[str, ...], dict[str, str], tuple[int, ...]]] = []
    descriptor_file = tmp_path / "descriptor"
    descriptor_file.write_text("not-a-private-key")
    descriptor = descriptor_file.open("rb")

    @contextmanager
    def session(*_args: object, **_kwargs: object) -> Iterator[DeployKeyTransportSession]:
        yield DeployKeyTransportSession(
            target=TARGET,
            repository_id=REPOSITORY_ID,
            key_fingerprint=PROOF.key_fingerprint,
            generation_id=PROOF.generation_id,
            git_executable=Path("/usr/bin/git"),
            work_directory=repository,
            ssh_command="/usr/bin/ssh -o IdentitiesOnly=yes -i /dev/fd/7",
            environment={"GIT_TERMINAL_PROMPT": "0"},
            private_descriptor=descriptor.fileno(),
        )

    monkeypatch.setattr(transport, "_validate_artifact_history", lambda **_kwargs: None)
    monkeypatch.setattr(transport, "open_repo_b_deploy_key_transport", session)
    monkeypatch.setattr(
        transport,
        "run_bounded_repo_b_git",
        lambda command, *, cwd, environment, pass_fds: (
            commands.append((command, environment, pass_fds)) or b""
        ),
    )
    try:
        assert getattr(transport, replace_name)(
            authorization=authorization,
            artifact=artifact,
            proof=PROOF,
            key_directory=tmp_path / "key",
        )
    finally:
        descriptor.close()

    assert len(commands) == 1
    command, environment, descriptors = commands[0]
    ref = f"refs/heads/{branch}"
    assert command[-3:] == (
        "ssh://git@github.com/Owner/Private-Home.git",
        f"{replacement}:{ref}",
        f"--force-with-lease={ref}:{LOG_HEAD}",
    )
    assert "credential.helper=" in command
    assert "protocol.file.allow=never" in command
    assert descriptors
    assert "GIT_ASKPASS" not in environment
    assert "token" not in " ".join(command).casefold()
