from __future__ import annotations

import shutil
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
    DatabaseHistoryReplacementTransportError,
    replace_database_history,
)
from ha_syncapp.database_history_replacement import (
    DatabaseHistoryReplacementAuthorization,
    authorize_database_history_replacement,
)
from ha_syncapp.github_repo import BranchHead

EXPECTED = "1" * 40
REPLACEMENT = "2" * 40
PRUNED = "0" * 40
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
    with patch("ha_syncapp.database_history_prewrite.fetch_trusted_branch_head", return_value=head):
        prewrite = reprove_database_history_prewrite(evidence=evidence, token="test-token")
    authorization = authorize_database_history_replacement(evidence=evidence, prewrite=prewrite)
    if branch != "database":
        return _forge(authorization, branch=branch)
    return authorization


def _forge(
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


def _make_repository(path: Path) -> Path:
    (path / ".git").mkdir()
    return path


def test_replacement_uses_exact_database_force_with_lease(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    calls: list[tuple[tuple[str, ...], Path, float]] = []

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd, timeout))
        return subprocess.CompletedProcess(command, 0, "", "")

    assert replace_database_history(
        authorization=_authorization(),
        repository=repository,
        replacement_head_sha=REPLACEMENT,
        timeout=12,
        runner=runner,
    )
    assert calls == [
        (
            (
                shutil.which("git"),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "push",
                "--porcelain",
                "--no-verify",
                "https://github.com/owner/private-repo.git",
                f"{REPLACEMENT}:refs/heads/database",
                f"--force-with-lease=refs/heads/database:{EXPECTED}",
            ),
            repository,
            12.0,
        )
    ]


def test_noop_runs_no_git_and_requires_no_repository_or_replacement() -> None:
    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("Git must not run for a no-op authorization")

    assert (
        replace_database_history(
            authorization=_authorization(pruned=False),
            repository=Path("relative-and-unused"),
            replacement_head_sha=None,
            runner=runner,
        )
        is False
    )


def test_replacement_requires_exact_authorization_type(tmp_path: Path) -> None:
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="authorization is invalid"):
        replace_database_history(
            authorization=cast(DatabaseHistoryReplacementAuthorization, object()),
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
        )


@pytest.mark.parametrize("branch", ["main", "candidate", "runtime", "logs", "Database", ""])
def test_replacement_rejects_non_database_authorization(tmp_path: Path, branch: str) -> None:
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="restricted to database"):
        replace_database_history(
            authorization=_authorization(branch=branch),
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
        )


@pytest.mark.parametrize(
    ("target", "repository_id"),
    [("invalid", 123), ("owner/repo", 0), ("owner/..", 123), ("owner/repo name", 123)],
)
def test_replacement_rejects_invalid_repository_identity(
    tmp_path: Path, target: str, repository_id: int
) -> None:
    authorization = _forge(_authorization(), target=target, repository_id=repository_id)
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="identity is invalid"):
        replace_database_history(
            authorization=authorization,
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
        )


@pytest.mark.parametrize(
    "authorization",
    [
        _forge(_authorization(), expected_head_sha="bad"),
        _forge(_authorization(), expected_head_sha="A" * 40),
        _forge(_authorization(), expected_head_sha="1" * 64),
        _forge(_authorization(), retained_shas=()),
        _forge(_authorization(), retained_shas=("3" * 40,)),
        _forge(_authorization(), retained_shas=(EXPECTED, EXPECTED)),
        _forge(_authorization(), pruned_shas=(PRUNED, PRUNED)),
        _forge(_authorization(), retained_shas=(EXPECTED, PRUNED)),
        _forge(_authorization(), pruned_shas=("bad",)),
    ],
)
def test_replacement_rejects_malformed_or_forged_authorization(
    tmp_path: Path, authorization: DatabaseHistoryReplacementAuthorization
) -> None:
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="authorization is invalid"):
        replace_database_history(
            authorization=authorization,
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
        )


@pytest.mark.parametrize("replacement", [None, "bad", EXPECTED, "A" * 40, "2" * 64])
def test_replacement_rejects_invalid_replacement_head(
    tmp_path: Path, replacement: str | None
) -> None:
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="head is invalid"):
        replace_database_history(
            authorization=_authorization(),
            repository=tmp_path,
            replacement_head_sha=replacement,
        )


def test_replacement_rejects_relative_missing_and_symlink_paths(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _make_repository(repository)
    link = tmp_path / "link"
    link.symlink_to(repository, target_is_directory=True)
    non_repository = tmp_path / "not-a-repository"
    non_repository.mkdir()

    for invalid in (Path("relative"), tmp_path / "missing", link, non_repository):
        with pytest.raises(DatabaseHistoryReplacementTransportError, match="path is invalid"):
            replace_database_history(
                authorization=_authorization(),
                repository=invalid,
                replacement_head_sha=REPLACEMENT,
            )


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 301, True])
def test_replacement_rejects_invalid_timeout(tmp_path: Path, timeout: float) -> None:
    repository = _make_repository(tmp_path)
    with pytest.raises(DatabaseHistoryReplacementTransportError, match="timeout is invalid"):
        replace_database_history(
            authorization=_authorization(),
            repository=repository,
            replacement_head_sha=REPLACEMENT,
            timeout=timeout,
        )


def test_stale_lease_failure_is_sanitized(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, "", "stale https://secret-token@github.com/owner/repo"
        )

    with pytest.raises(
        DatabaseHistoryReplacementTransportError, match="replacement was rejected"
    ) as caught:
        replace_database_history(
            authorization=_authorization(),
            repository=repository,
            replacement_head_sha=REPLACEMENT,
            runner=runner,
        )
    assert "secret-token" not in str(caught.value)


def test_transport_exception_and_invalid_runner_result_are_sanitized(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("https://secret-token@github.com/owner/repo")

    with pytest.raises(
        DatabaseHistoryReplacementTransportError, match="transport failed"
    ) as caught:
        replace_database_history(
            authorization=_authorization(),
            repository=repository,
            replacement_head_sha=REPLACEMENT,
            runner=fail,
        )
    assert "secret-token" not in str(caught.value)

    def invalid_result(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return cast(subprocess.CompletedProcess[str], object())

    with pytest.raises(DatabaseHistoryReplacementTransportError, match="transport failed"):
        replace_database_history(
            authorization=_authorization(),
            repository=repository,
            replacement_head_sha=REPLACEMENT,
            runner=invalid_result,
        )
