from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from ha_syncapp.database_history_replacement import authorize_database_history_replacement
from ha_syncapp.github_repo import BranchHead

EXPECTED = "1" * 40
PRUNED = "0" * 40
TREE = "a" * 40
REPLACEMENT = "2" * 40
REFERENCE = datetime(2026, 9, 13, tzinfo=UTC)


def _authorization():
    head = BranchHead("owner/private-repo", 123, "database", EXPECTED)
    evidence = validate_trusted_database_history_evidence(
        branch_head=head,
        records=(
            DatabaseHistoryRecord(EXPECTED, REFERENCE - timedelta(days=1), (PRUNED,)),
            DatabaseHistoryRecord(PRUNED, REFERENCE - timedelta(days=10), ()),
        ),
        reference_time=REFERENCE,
        retention_days=7,
    )
    with patch(
        "ha_syncapp.database_history_prewrite.fetch_trusted_branch_head",
        return_value=head,
    ):
        prewrite = reprove_database_history_prewrite(evidence=evidence, token="test-token")
    return authorize_database_history_replacement(evidence=evidence, prewrite=prewrite)


def _make_repository(path: Path) -> Path:
    (path / ".git").mkdir()
    return path


def _history_read(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    if command[-1] == EXPECTED:
        payload = (
            f"tree {TREE}\n"
            f"parent {PRUNED}\n"
            "author SyncApp <syncapp@example.invalid> 1789250000 +0000\n"
            "committer SyncApp <syncapp@example.invalid> 1789250000 +0000\n"
            "\nRecorder snapshot\n"
        )
    elif command[-1] == REPLACEMENT:
        payload = (
            f"tree {TREE}\n"
            "author SyncApp <syncapp@example.invalid> 1789250000 +0000\n"
            "committer SyncApp <syncapp@example.invalid> 1789250000 +0000\n"
            "\nRecorder snapshot\n"
        )
    else:
        raise AssertionError(command)
    return subprocess.CompletedProcess(command, 0, payload, "")


def _build_artifact(repository: Path) -> DatabaseHistoryReplacementArtifact:
    authorization = _authorization()

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if "cat-file" in command:
            return _history_read(command)
        assert "hash-object" in command
        commit_file = Path(command[-1])
        rebuilt = commit_file.read_text(encoding="utf-8")
        assert f"tree {TREE}\n" in rebuilt
        assert "parent " not in rebuilt
        assert "Recorder snapshot\n" in rebuilt
        return subprocess.CompletedProcess(command, 0, f"{REPLACEMENT}\n", "")

    return build_database_history_replacement(
        authorization=authorization,
        repository=repository,
        runner=runner,
    )


def test_builder_severs_pruned_ancestry_and_binds_exact_retained_content(
    tmp_path: Path,
) -> None:
    repository = _make_repository(tmp_path)
    artifact = _build_artifact(repository)

    assert type(artifact) is DatabaseHistoryReplacementArtifact
    assert artifact.replacement_head_sha == REPLACEMENT
    assert artifact.expected_head_sha == EXPECTED
    assert artifact.retained_shas == (EXPECTED,)
    assert artifact.repository == repository


def test_artifact_cannot_be_constructed_by_arbitrary_caller() -> None:
    with pytest.raises(TypeError, match="must be produced"):
        DatabaseHistoryReplacementArtifact()


def test_transport_reproves_private_repo_identity_and_exact_head_before_push(
    tmp_path: Path,
) -> None:
    repository = _make_repository(tmp_path)
    authorization = _authorization()
    artifact = _build_artifact(repository)
    current = BranchHead("owner/private-repo", 123, "database", EXPECTED)
    pushes: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if "cat-file" in command:
            return _history_read(command)
        pushes.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch(
        "ha_syncapp.database_history_replace_transport.fetch_trusted_branch_head",
        return_value=current,
    ) as fetch:
        assert replace_database_history(
            authorization=authorization,
            artifact=artifact,
            token="test-token",
            runner=runner,
        )

    fetch.assert_called_once_with(
        "owner/private-repo",
        "test-token",
        expected_id=123,
        branch="database",
    )
    assert len(pushes) == 1
    assert pushes[0][-1] == f"--force-with-lease=refs/heads/database:{EXPECTED}"
    assert f"{REPLACEMENT}:refs/heads/database" in pushes[0]


def test_transport_blocks_moved_head_without_attempting_push(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    authorization = _authorization()
    artifact = _build_artifact(repository)
    moved = BranchHead("owner/private-repo", 123, "database", "3" * 40)

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if "cat-file" in command:
            return _history_read(command)
        raise AssertionError("push must not run after the database head moves")

    with patch(
        "ha_syncapp.database_history_replace_transport.fetch_trusted_branch_head",
        return_value=moved,
    ):
        with pytest.raises(
            DatabaseHistoryReplacementTransportError,
            match="changed before replacement",
        ):
            replace_database_history(
                authorization=authorization,
                artifact=artifact,
                token="test-token",
                runner=runner,
            )


def test_transport_rejects_artifact_bound_to_another_repository(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path / "one")
    other = _make_repository(tmp_path / "two")
    authorization = _authorization()
    artifact = _build_artifact(repository)

    with pytest.raises(
        DatabaseHistoryReplacementTransportError,
        match="artifact is invalid",
    ):
        replace_database_history(
            authorization=authorization,
            artifact=artifact,
            token="test-token",
            repository=other,
        )


def test_builder_does_not_touch_refs_or_push(tmp_path: Path) -> None:
    repository = _make_repository(tmp_path)
    authorization = _authorization()
    seen: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        if "cat-file" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                f"tree {TREE}\nparent {PRUNED}\nauthor A <a@b> 1 +0000\ncommitter A <a@b> 1 +0000\n\nmessage\n",
                "",
            )
        if "hash-object" in command:
            return subprocess.CompletedProcess(command, 0, f"{REPLACEMENT}\n", "")
        raise AssertionError(command)

    build_database_history_replacement(
        authorization=authorization,
        repository=repository,
        runner=runner,
    )

    assert seen
    assert all("push" not in command and "update-ref" not in command for command in seen)
    assert shutil.which("git") is not None
