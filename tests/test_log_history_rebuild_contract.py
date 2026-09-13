from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ha_syncapp.log_history_replace_transport import (
    LogHistoryReplacementArtifact,
    LogHistoryReplacementTransportError,
    build_log_history_replacement,
    replace_logs_history,
)
from ha_syncapp.log_history_replacement import LogHistoryReplacementAuthorization


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


def _repository(tmp_path: Path) -> tuple[Path, tuple[str, str, str]]:
    repository = tmp_path / "logs-staging"
    repository.mkdir()
    _git(repository, "init")
    shas: list[str] = []
    for index in range(3):
        (repository / "logs.jsonl").write_text(f"entry-{index}\n", encoding="utf-8")
        _git(repository, "add", "logs.jsonl")
        _git(repository, "commit", "-m", f"logs-{index}")
        shas.append(_git(repository, "rev-parse", "HEAD"))
    return repository, (shas[2], shas[1], shas[0])


def _authorization(shas: tuple[str, str, str]) -> LogHistoryReplacementAuthorization:
    return LogHistoryReplacementAuthorization(
        target="owner/private-repo",
        repository_id=123,
        branch="logs",
        expected_head_sha=shas[0],
        retained_shas=(shas[0], shas[1]),
        pruned_shas=(shas[2],),
    )


def test_builder_returns_sealed_artifact_and_severs_only_pruned_ancestry(
    tmp_path: Path,
) -> None:
    repository, shas = _repository(tmp_path)
    authorization = _authorization(shas)

    artifact = build_log_history_replacement(
        authorization=authorization,
        repository=repository,
    )

    assert type(artifact) is LogHistoryReplacementArtifact
    assert artifact.repository == repository.resolve()
    assert artifact.target == authorization.target
    assert artifact.repository_id == authorization.repository_id
    assert artifact.branch == "logs"
    assert artifact.expected_head_sha == shas[0]
    assert artifact.retained_shas == (shas[0], shas[1])
    assert artifact.replacement_head_sha != shas[0]

    replacement_parent = _git(
        repository, "show", "-s", "--format=%P", artifact.replacement_head_sha
    )
    assert replacement_parent
    assert _git(repository, "show", "-s", "--format=%P", replacement_parent) == ""

    assert _git(repository, "show", "-s", "--format=%T", artifact.replacement_head_sha) == _git(
        repository, "show", "-s", "--format=%T", shas[0]
    )
    assert _git(repository, "show", "-s", "--format=%T", replacement_parent) == _git(
        repository, "show", "-s", "--format=%T", shas[1]
    )


def test_artifact_cannot_be_constructed_from_arbitrary_replacement_sha(tmp_path: Path) -> None:
    repository, shas = _repository(tmp_path)

    with pytest.raises(TypeError):
        LogHistoryReplacementArtifact(
            repository=repository,
            target="owner/private-repo",
            repository_id=123,
            branch="logs",
            expected_head_sha=shas[0],
            retained_shas=(shas[0], shas[1]),
            replacement_head_sha="f" * 40,
        )


def test_publication_requires_builder_artifact_not_raw_sha(tmp_path: Path) -> None:
    repository, shas = _repository(tmp_path)
    authorization = _authorization(shas)

    with pytest.raises(TypeError):
        replace_logs_history(  # type: ignore[call-arg]
            authorization=authorization,
            repository=repository,
            replacement_head_sha="f" * 40,
        )


def test_builder_rejects_repository_without_authorized_history(tmp_path: Path) -> None:
    repository, shas = _repository(tmp_path)
    authorization = _authorization(shas)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init")

    with pytest.raises(LogHistoryReplacementTransportError):
        build_log_history_replacement(authorization=authorization, repository=other)
