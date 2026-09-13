from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from ha_syncapp.log_history_replace_transport import (
    LogHistoryReplacementTransportError,
    build_log_history_replacement,
    replace_logs_history,
)
from ha_syncapp.log_history_replacement import LogHistoryReplacementAuthorization

EXPECTED = "1" * 40


def _git(repository: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(  # nosec B603 B607
        ("git", *args),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
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


def _repository(tmp_path: Path) -> tuple[Path, LogHistoryReplacementAuthorization]:
    repository = tmp_path / "logs-staging"
    repository.mkdir()
    _git(repository, "init")
    shas: list[str] = []
    for index in range(3):
        (repository / "logs.jsonl").write_text(
            f"entry-{index}\n",
            encoding="utf-8",
        )
        _git(repository, "add", "logs.jsonl")
        _git(repository, "commit", "-m", f"logs-{index}")
        shas.append(_git(repository, "rev-parse", "HEAD"))
    newest, middle, oldest = shas[2], shas[1], shas[0]
    authorization = LogHistoryReplacementAuthorization(
        target="owner/private-repo",
        repository_id=123,
        branch="logs",
        expected_head_sha=newest,
        retained_shas=(newest, middle),
        pruned_shas=(oldest,),
    )
    return repository, authorization


def _authorization(
    *,
    pruned: bool = True,
    branch: str = "logs",
) -> LogHistoryReplacementAuthorization:
    return LogHistoryReplacementAuthorization(
        target="owner/private-repo",
        repository_id=123,
        branch=branch,
        expected_head_sha=EXPECTED,
        retained_shas=(EXPECTED,),
        pruned_shas=(("0" * 40),) if pruned else (),
    )


def _delegating_runner(
    *,
    push_returncode: int = 0,
    push_stderr: str = "",
    push_error: OSError | None = None,
    calls: list[tuple[tuple[str, ...], Path, float]] | None = None,
):
    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if "push" in command:
            if calls is not None:
                calls.append((command, cwd, timeout))
            if push_error is not None:
                raise push_error
            return subprocess.CompletedProcess(
                command,
                push_returncode,
                "",
                push_stderr,
            )
        return subprocess.run(  # nosec B603
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )

    return runner


def test_replacement_uses_exact_logs_force_with_lease(tmp_path: Path) -> None:
    repository, authorization = _repository(tmp_path)
    artifact = build_log_history_replacement(
        authorization=authorization,
        repository=repository,
    )
    calls: list[tuple[tuple[str, ...], Path, float]] = []

    assert (
        replace_logs_history(
            authorization=authorization,
            artifact=artifact,
            timeout=12,
            runner=_delegating_runner(calls=calls),
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
                f"{artifact.replacement_head_sha}:refs/heads/logs",
                (
                    "--force-with-lease=refs/heads/logs:"
                    f"{authorization.expected_head_sha}"
                ),
            ),
            repository,
            12.0,
        )
    ]


@pytest.mark.parametrize(
    ("target", "repository_id"),
    [
        ("invalid", 123),
        ("owner/repo", 0),
        ("owner/..", 123),
        ("owner/repo name", 123),
    ],
)
def test_replacement_rejects_invalid_authorized_repository_identity(
    target: str,
    repository_id: int,
) -> None:
    authorization = replace(
        _authorization(),
        target=target,
        repository_id=repository_id,
    )

    with pytest.raises(
        LogHistoryReplacementTransportError,
        match="identity is invalid",
    ):
        replace_logs_history(authorization=authorization)


def test_replacement_rejects_symlink_repository(tmp_path: Path) -> None:
    repository, authorization = _repository(tmp_path)
    artifact = build_log_history_replacement(
        authorization=authorization,
        repository=repository,
    )
    link = tmp_path / "link"
    link.symlink_to(repository, target_is_directory=True)

    with pytest.raises(
        LogHistoryReplacementTransportError,
        match="path is invalid",
    ):
        replace_logs_history(
            authorization=authorization,
            artifact=artifact,
            repository=link,
        )


def test_noop_authorization_runs_no_transport() -> None:
    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("transport must not run")

    assert (
        replace_logs_history(
            authorization=_authorization(pruned=False),
            runner=runner,
        )
        is False
    )


def test_replacement_rejects_non_logs_authorization() -> None:
    with pytest.raises(
        LogHistoryReplacementTransportError,
        match="restricted to logs",
    ):
        replace_logs_history(authorization=_authorization(branch="main"))


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("nan"), float("inf"), 301, True],
)
def test_replacement_rejects_invalid_or_unbounded_timeout(
    tmp_path: Path,
    timeout: float,
) -> None:
    repository, authorization = _repository(tmp_path)
    artifact = build_log_history_replacement(
        authorization=authorization,
        repository=repository,
    )

    with pytest.raises(
        LogHistoryReplacementTransportError,
        match="timeout is invalid",
    ):
        replace_logs_history(
            authorization=authorization,
            artifact=artifact,
            timeout=timeout,
        )


def test_stale_lease_failure_is_sanitized(tmp_path: Path) -> None:
    repository, authorization = _repository(tmp_path)
    artifact = build_log_history_replacement(
        authorization=authorization,
        repository=repository,
    )
    runner = _delegating_runner(
        push_returncode=1,
        push_stderr=(
            "stale info containing "
            "https://secret-token@github.com/owner/repo"
        ),
    )

    with pytest.raises(
        LogHistoryReplacementTransportError,
        match="history replacement was rejected",
    ) as caught:
        replace_logs_history(
            authorization=authorization,
            artifact=artifact,
            runner=runner,
        )

    assert "secret-token" not in str(caught.value)


def test_transport_exception_is_sanitized(tmp_path: Path) -> None:
    repository, authorization = _repository(tmp_path)
    artifact = build_log_history_replacement(
        authorization=authorization,
        repository=repository,
    )
    runner = _delegating_runner(
        push_error=OSError("https://secret-token@github.com/owner/repo")
    )

    with pytest.raises(
        LogHistoryReplacementTransportError,
        match="transport failed",
    ) as caught:
        replace_logs_history(
            authorization=authorization,
            artifact=artifact,
            runner=runner,
        )

    assert "secret-token" not in str(caught.value)
