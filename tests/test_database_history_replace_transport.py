from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from ha_syncapp.database_history_evidence import (
    DatabaseHistoryRecord,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_history_prewrite import reprove_database_history_prewrite
from ha_syncapp.database_history_replace_transport import (
    DatabaseHistoryReplacementArtifact,
    DatabaseHistoryReplacementTransportError,
    build_database_history_replacement,
    replace_database_history,
)
from ha_syncapp.database_history_replacement import (
    DatabaseHistoryReplacementAuthorization,
    authorize_database_history_replacement,
)
from ha_syncapp.github_repo import BranchHead, RepositoryVerificationError

EXPECTED = "1" * 40
REPLACEMENT = "2" * 40
PRUNED = "0" * 40
TREE = "a" * 40
REFERENCE = datetime(2026, 9, 13, tzinfo=UTC)


def _authorization(
    *, pruned: bool = True, branch: str = "database"
) -> DatabaseHistoryReplacementAuthorization:
    root_age = timedelta(days=10 if pruned else 6)
    head = BranchHead("owner/private-repo", 123, "database", EXPECTED)
    evidence = validate_trusted_database_history_evidence(
        branch_head=head,
        records=(
            DatabaseHistoryRecord(EXPECTED, REFERENCE - timedelta(days=1), (PRUNED,)),
            DatabaseHistoryRecord(PRUNED, REFERENCE - root_age, ()),
        ),
        reference_time=REFERENCE,
        retention_days=7,
    )
    with patch(
        "ha_syncapp.database_history_prewrite.fetch_trusted_branch_head",
        return_value=head,
    ):
        prewrite = reprove_database_history_prewrite(evidence=evidence, token="test-token")
    authorization = authorize_database_history_replacement(evidence=evidence, prewrite=prewrite)
    if branch != "database":
        return _forge_authorization(authorization, branch=branch)
    return authorization


def _forge_authorization(
    authorization: DatabaseHistoryReplacementAuthorization, **changes: object
) -> DatabaseHistoryReplacementAuthorization:
    forged = object.__new__(DatabaseHistoryReplacementAuthorization)
    for field in (
        "target",
        "repository_id",
        "branch",
        "expected_head_sha",
        "retained_shas",
        "pruned_shas",
    ):
        object.__setattr__(forged, field, changes.get(field, getattr(authorization, field)))
    return forged


def _forge_artifact(
    artifact: DatabaseHistoryReplacementArtifact, **changes: object
) -> DatabaseHistoryReplacementArtifact:
    forged = object.__new__(DatabaseHistoryReplacementArtifact)
    for field in (
        "repository",
        "target",
        "repository_id",
        "branch",
        "expected_head_sha",
        "retained_shas",
        "replacement_head_sha",
    ):
        object.__setattr__(forged, field, changes.get(field, getattr(artifact, field)))
    return forged


def _make_repository(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _artifact(
    repository: Path,
    authorization: DatabaseHistoryReplacementAuthorization | None = None,
) -> DatabaseHistoryReplacementArtifact:
    authorization = authorization or _authorization()

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if "cat-file" in command:
            payload = (
                f"tree {TREE}\n"
                f"parent {PRUNED}\n"
                "author SyncApp <syncapp@example.invalid> 1789250000 +0000\n"
                "committer SyncApp <syncapp@example.invalid> 1789250000 +0000\n"
                "\nRecorder snapshot\n"
            )
            return subprocess.CompletedProcess(command, 0, payload, "")
        if "hash-object" in command:
            return subprocess.CompletedProcess(command, 0, f"{REPLACEMENT}\n", "")
        raise AssertionError(command)

    return build_database_history_replacement(
        authorization=authorization,
        repository=repository,
        runner=runner,
    )


def test_replacement_uses_exact_database_force_with_lease_after_reproof(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path / "repository")
    authorization = _authorization()
    artifact = _artifact(repository, authorization)
    calls: list[tuple[tuple[str, ...], Path, float]] = []

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd, timeout))
        return subprocess.CompletedProcess(command, 0, "", "")

    current = BranchHead("owner/private-repo", 123, "database", EXPECTED)
    with patch(
        "ha_syncapp.database_history_replace_transport.fetch_trusted_branch_head",
        return_value=current,
    ):
        assert replace_database_history(
            authorization=authorization,
            artifact=artifact,
            token="test-token",
            timeout=12,
            runner=runner,
        )

    command, cwd, timeout = calls[0]
    assert command[-1] == f"--force-with-lease=refs/heads/database:{EXPECTED}"
    assert f"{REPLACEMENT}:refs/heads/database" in command
    assert "https://github.com/owner/private-repo.git" in command
    assert cwd == repository
    assert timeout == 12.0


def test_noop_runs_no_git_and_requires_no_token_repository_or_artifact() -> None:
    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("Git must not run for a no-op authorization")

    assert (
        replace_database_history(
            authorization=_authorization(pruned=False),
            runner=runner,
        )
        is False
    )


def test_replacement_requires_exact_authorization_type() -> None:
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="authorization is invalid"):
        replace_database_history(
            authorization=cast(DatabaseHistoryReplacementAuthorization, object()),
        )


@pytest.mark.parametrize("branch", ["main", "candidate", "runtime", "logs", "Database", ""])
def test_replacement_rejects_non_database_authorization(branch: str) -> None:
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="restricted to database"):
        replace_database_history(authorization=_authorization(branch=branch))


@pytest.mark.parametrize(
    ("target", "repository_id"),
    [("invalid", 123), ("owner/repo", 0), ("owner/..", 123), ("owner/repo name", 123)],
)
def test_replacement_rejects_invalid_repository_identity(
    target: str, repository_id: int
) -> None:
    authorization = _forge_authorization(
        _authorization(), target=target, repository_id=repository_id
    )
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="identity is invalid"):
        replace_database_history(authorization=authorization)


@pytest.mark.parametrize(
    "authorization",
    [
        _forge_authorization(_authorization(), expected_head_sha="bad"),
        _forge_authorization(_authorization(), expected_head_sha="A" * 40),
        _forge_authorization(_authorization(), retained_shas=()),
        _forge_authorization(_authorization(), retained_shas=("3" * 40,)),
        _forge_authorization(_authorization(), retained_shas=(EXPECTED, EXPECTED)),
        _forge_authorization(_authorization(), pruned_shas=(PRUNED, PRUNED)),
        _forge_authorization(_authorization(), retained_shas=(EXPECTED, PRUNED)),
        _forge_authorization(_authorization(), pruned_shas=("bad",)),
    ],
)
def test_replacement_rejects_malformed_or_forged_authorization(
    authorization: DatabaseHistoryReplacementAuthorization,
) -> None:
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="authorization is invalid"):
        replace_database_history(authorization=authorization)


def test_replacement_rejects_forged_artifact(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path / "repository")
    authorization = _authorization()
    artifact = _artifact(repository, authorization)

    for forged in (
        _forge_artifact(artifact, target="other/repo"),
        _forge_artifact(artifact, repository_id=999),
        _forge_artifact(artifact, branch="main"),
        _forge_artifact(artifact, expected_head_sha="3" * 40),
        _forge_artifact(artifact, retained_shas=("3" * 40,)),
        _forge_artifact(artifact, replacement_head_sha="bad"),
        _forge_artifact(artifact, replacement_head_sha=EXPECTED),
    ):
        with pytest.raises(DatabaseHistoryReplacementTransportError, match="artifact is invalid"):
            replace_database_history(
                authorization=authorization,
                artifact=forged,
                token="test-token",
            )


def test_replacement_rejects_relative_missing_and_symlink_artifact_paths(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path / "repository")
    authorization = _authorization()
    artifact = _artifact(repository, authorization)
    link = tmp_path / "link"
    link.symlink_to(repository, target_is_directory=True)

    for invalid in (Path("relative"), tmp_path / "missing", link):
        forged = _forge_artifact(artifact, repository=invalid)
        with pytest.raises(DatabaseHistoryReplacementTransportError, match="path is invalid"):
            replace_database_history(
                authorization=authorization,
                artifact=forged,
                token="test-token",
            )


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 301, True])
def test_replacement_rejects_invalid_timeout(tmp_path: Path, timeout: float) -> None:
    repository = _make_repository(tmp_path / "repository")
    authorization = _authorization()
    artifact = _artifact(repository, authorization)
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="timeout is invalid"):
        replace_database_history(
            authorization=authorization,
            artifact=artifact,
            token="test-token",
            timeout=timeout,
        )


def test_mutation_time_repo_verification_failure_is_sanitized(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path / "repository")
    authorization = _authorization()
    artifact = _artifact(repository, authorization)

    with patch(
        "ha_syncapp.database_history_replace_transport.fetch_trusted_branch_head",
        side_effect=RepositoryVerificationError("secret-token"),
    ):
        with pytest.raises(
            DatabaseHistoryReplacementTransportError,
            match="repository verification failed",
        ) as caught:
            replace_database_history(
                authorization=authorization,
                artifact=artifact,
                token="secret-token",
            )
    assert "secret-token" not in str(caught.value)


def test_stale_lease_failure_is_sanitized(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path / "repository")
    authorization = _authorization()
    artifact = _artifact(repository, authorization)
    current = BranchHead("owner/private-repo", 123, "database", EXPECTED)

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, "", "stale https://secret-token@github.com/owner/repo"
        )

    with patch(
        "ha_syncapp.database_history_replace_transport.fetch_trusted_branch_head",
        return_value=current,
    ):
        with pytest.raises(
            DatabaseHistoryReplacementTransportError, match="replacement was rejected"
        ) as caught:
            replace_database_history(
                authorization=authorization,
                artifact=artifact,
                token="test-token",
                runner=runner,
            )
    assert "secret-token" not in str(caught.value)


def test_transport_exception_and_invalid_runner_result_are_sanitized(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path / "repository")
    authorization = _authorization()
    artifact = _artifact(repository, authorization)
    current = BranchHead("owner/private-repo", 123, "database", EXPECTED)

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("https://secret-token@github.com/owner/repo")

    with patch(
        "ha_syncapp.database_history_replace_transport.fetch_trusted_branch_head",
        return_value=current,
    ):
        with pytest.raises(
            DatabaseHistoryReplacementTransportError, match="transport failed"
        ) as caught:
            replace_database_history(
                authorization=authorization,
                artifact=artifact,
                token="test-token",
                runner=fail,
            )
    assert "secret-token" not in str(caught.value)

    def invalid_result(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return cast(subprocess.CompletedProcess[str], object())

    with patch(
        "ha_syncapp.database_history_replace_transport.fetch_trusted_branch_head",
        return_value=current,
    ):
        with pytest.raises(DatabaseHistoryReplacementTransportError, match="transport failed"):
            replace_database_history(
                authorization=authorization,
                artifact=artifact,
                token="test-token",
                runner=invalid_result,
            )
