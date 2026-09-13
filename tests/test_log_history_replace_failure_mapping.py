from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from ha_syncapp.log_history_replace_transport import (
    LogHistoryReplacementFailureKind,
    LogHistoryReplacementTransportError,
    build_log_history_replacement,
    replace_logs_history,
)
from ha_syncapp.log_history_replacement import LogHistoryReplacementAuthorization


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(  # nosec B603 B607
        ("git", *args),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repository),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "SyncApp Test",
            "GIT_AUTHOR_EMAIL": "syncapp@example.invalid",
            "GIT_COMMITTER_NAME": "SyncApp Test",
            "GIT_COMMITTER_EMAIL": "syncapp@example.invalid",
        },
    )
    return result.stdout.strip()


def _replacement(tmp_path: Path):
    repository = tmp_path / "logs-staging"
    repository.mkdir()
    _git(repository, "init")
    shas: list[str] = []
    for index in range(3):
        (repository / "logs.jsonl").write_text(f"entry-{index}\n", encoding="utf-8")
        _git(repository, "add", "logs.jsonl")
        _git(repository, "commit", "-m", f"logs-{index}")
        shas.append(_git(repository, "rev-parse", "HEAD"))
    authorization = LogHistoryReplacementAuthorization(
        target="owner/private-repo",
        repository_id=123,
        branch="logs",
        expected_head_sha=shas[2],
        retained_shas=(shas[2], shas[1]),
        pruned_shas=(shas[0],),
    )
    artifact = build_log_history_replacement(
        authorization=authorization,
        repository=repository,
    )
    return repository, authorization, artifact


def _runner(*, failure: BaseException | None = None, returncode: int = 0):
    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if "push" in command:
            if failure is not None:
                raise failure
            return subprocess.CompletedProcess(command, returncode, "", "rejected")
        return subprocess.run(  # nosec B603
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )

    return runner


def test_transport_exception_is_transient_and_retryable(tmp_path: Path) -> None:
    _repository, authorization, artifact = _replacement(tmp_path)

    with pytest.raises(LogHistoryReplacementTransportError) as caught:
        replace_logs_history(
            authorization=authorization,
            artifact=artifact,
            runner=_runner(failure=OSError("secret transport detail")),
        )

    assert caught.value.kind is LogHistoryReplacementFailureKind.TRANSIENT
    assert caught.value.retryable is True
    assert "secret transport detail" not in str(caught.value)


def test_remote_push_rejection_is_non_retryable_rejected(tmp_path: Path) -> None:
    _repository, authorization, artifact = _replacement(tmp_path)

    with pytest.raises(LogHistoryReplacementTransportError) as caught:
        replace_logs_history(
            authorization=authorization,
            artifact=artifact,
            runner=_runner(returncode=1),
        )

    assert caught.value.kind is LogHistoryReplacementFailureKind.REJECTED
    assert caught.value.retryable is False
