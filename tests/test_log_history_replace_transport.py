from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from ha_syncapp.log_history_replace_transport import (
    LogHistoryReplacementTransportError,
    replace_logs_history,
)
from ha_syncapp.log_history_replacement import LogHistoryReplacementAuthorization

EXPECTED = "1" * 40
REPLACEMENT = "2" * 40


def _authorization(
    *, pruned: bool = True, branch: str = "logs"
) -> LogHistoryReplacementAuthorization:
    return LogHistoryReplacementAuthorization(
        target="owner/private-repo",
        repository_id=123,
        branch=branch,
        expected_head_sha=EXPECTED,
        retained_shas=(EXPECTED,),
        pruned_shas=(("0" * 40),) if pruned else (),
    )


def test_replacement_uses_exact_logs_force_with_lease(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], Path, float]] = []

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd, timeout))
        return subprocess.CompletedProcess(command, 0, "", "")

    assert (
        replace_logs_history(
            authorization=_authorization(),
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
            timeout=12,
            runner=runner,
        )
        is True
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
                f"{REPLACEMENT}:refs/heads/logs",
                f"--force-with-lease=refs/heads/logs:{EXPECTED}",
            ),
            tmp_path,
            12.0,
        )
    ]


@pytest.mark.parametrize(
    ("target", "repository_id"),
    [("invalid", 123), ("owner/repo", 0), ("owner/..", 123), ("owner/repo name", 123)],
)
def test_replacement_rejects_invalid_authorized_repository_identity(
    tmp_path: Path,
    target: str,
    repository_id: int,
) -> None:
    authorization = replace(
        _authorization(),
        target=target,
        repository_id=repository_id,
    )

    with pytest.raises(LogHistoryReplacementTransportError, match="identity is invalid"):
        replace_logs_history(
            authorization=authorization,
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
        )


def test_replacement_rejects_symlink_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    link = tmp_path / "link"
    link.symlink_to(repository, target_is_directory=True)

    with pytest.raises(LogHistoryReplacementTransportError, match="path is invalid"):
        replace_logs_history(
            authorization=_authorization(),
            repository=link,
            replacement_head_sha=REPLACEMENT,
        )


def test_noop_authorization_runs_no_transport(tmp_path: Path) -> None:
    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("transport must not run")

    assert (
        replace_logs_history(
            authorization=_authorization(pruned=False),
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
            runner=runner,
        )
        is False
    )


def test_replacement_rejects_non_logs_authorization(tmp_path: Path) -> None:
    with pytest.raises(LogHistoryReplacementTransportError, match="restricted to logs"):
        replace_logs_history(
            authorization=_authorization(branch="main"),
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
        )


@pytest.mark.parametrize("replacement", ["bad", EXPECTED, "A" * 40, "2" * 64])
def test_replacement_rejects_invalid_replacement_head(tmp_path: Path, replacement: str) -> None:
    with pytest.raises(LogHistoryReplacementTransportError, match="head is invalid"):
        replace_logs_history(
            authorization=_authorization(),
            repository=tmp_path,
            replacement_head_sha=replacement,
        )


def test_stale_lease_failure_is_sanitized(tmp_path: Path) -> None:
    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "stale info containing https://secret-token@github.com/owner/repo",
        )

    with pytest.raises(
        LogHistoryReplacementTransportError, match="history replacement was rejected"
    ) as caught:
        replace_logs_history(
            authorization=_authorization(),
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
            runner=runner,
        )

    assert "secret-token" not in str(caught.value)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 301, True])
def test_replacement_rejects_invalid_or_unbounded_timeout(
    tmp_path: Path,
    timeout: float,
) -> None:
    with pytest.raises(LogHistoryReplacementTransportError, match="timeout is invalid"):
        replace_logs_history(
            authorization=_authorization(),
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
            timeout=timeout,
        )


def test_transport_exception_is_sanitized(tmp_path: Path) -> None:
    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("https://secret-token@github.com/owner/repo")

    with pytest.raises(LogHistoryReplacementTransportError, match="transport failed") as caught:
        replace_logs_history(
            authorization=_authorization(),
            repository=tmp_path,
            replacement_head_sha=REPLACEMENT,
            runner=runner,
        )

    assert "secret-token" not in str(caught.value)
