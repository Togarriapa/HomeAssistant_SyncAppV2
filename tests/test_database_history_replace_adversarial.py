from __future__ import annotations

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
    DatabaseHistoryReplacementFailureKind,
    DatabaseHistoryReplacementTransportError,
    replace_database_history,
)
from ha_syncapp.database_history_replacement import authorize_database_history_replacement
from ha_syncapp.github_repo import BranchHead

EXPECTED = "1" * 40
PRUNED = "0" * 40
FORGED = "3" * 40
AUTHORIZED_TREE = "a" * 40
FORGED_TREE = "b" * 40
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


def _forged_artifact(repository: Path) -> DatabaseHistoryReplacementArtifact:
    artifact = object.__new__(DatabaseHistoryReplacementArtifact)
    object.__setattr__(artifact, "repository", repository)
    object.__setattr__(artifact, "target", "owner/private-repo")
    object.__setattr__(artifact, "repository_id", 123)
    object.__setattr__(artifact, "branch", "database")
    object.__setattr__(artifact, "expected_head_sha", EXPECTED)
    object.__setattr__(artifact, "retained_shas", (EXPECTED,))
    object.__setattr__(artifact, "replacement_head_sha", FORGED)
    return artifact


def test_valid_looking_forged_sha_cannot_gain_database_publication_authority(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    authorization = _authorization()
    artifact = _forged_artifact(tmp_path)
    pushed = False

    def runner(
        command: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        nonlocal pushed
        if "push" in command:
            pushed = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if "cat-file" in command and command[-1] == EXPECTED:
            payload = (
                f"tree {AUTHORIZED_TREE}\n"
                f"parent {PRUNED}\n"
                "author A <a@b> 1 +0000\n"
                "committer A <a@b> 1 +0000\n"
                "\noriginal\n"
            )
            return subprocess.CompletedProcess(command, 0, payload, "")
        if "cat-file" in command and command[-1] == FORGED:
            payload = (
                f"tree {FORGED_TREE}\n"
                "author X <x@y> 1 +0000\n"
                "committer X <x@y> 1 +0000\n"
                "\nforged\n"
            )
            return subprocess.CompletedProcess(command, 0, payload, "")
        raise AssertionError(command)

    current = BranchHead("owner/private-repo", 123, "database", EXPECTED)
    with patch(
        "ha_syncapp.database_history_replace_transport.fetch_trusted_branch_head",
        return_value=current,
    ), pytest.raises(
        DatabaseHistoryReplacementTransportError,
        match="artifact is invalid",
    ) as caught:
        replace_database_history(
            authorization=authorization,
            artifact=artifact,
            token="test-token",
            runner=runner,
        )

    assert caught.value.kind is DatabaseHistoryReplacementFailureKind.INVALID
    assert not caught.value.retryable
    assert pushed is False
