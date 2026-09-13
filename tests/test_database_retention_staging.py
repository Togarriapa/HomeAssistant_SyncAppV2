from __future__ import annotations

import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.database_history_evidence import (
    DatabaseHistoryRecord,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_retention_staging import (
    DatabaseRetentionStagingError,
    prepare_database_history_staging,
)
from ha_syncapp.github_repo import BranchHead

TARGET = "Owner/Private-Home"
TOKEN = "github-secret-sentinel"
HEAD = "1" * 40
ROOT = "0" * 40
NOW = datetime(2026, 9, 13, tzinfo=UTC)


def _evidence():
    return validate_trusted_database_history_evidence(
        branch_head=BranchHead(TARGET, 123, "database", HEAD),
        records=(
            DatabaseHistoryRecord(HEAD, NOW - timedelta(days=1), (ROOT,)),
            DatabaseHistoryRecord(ROOT, NOW - timedelta(days=10), ()),
        ),
        reference_time=NOW,
        retention_days=7,
    )


def test_staging_fetches_exact_head_with_ephemeral_private_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(command, *, cwd, env, **kwargs):
        command = tuple(command)
        observed.append((command, dict(env)))
        assert TOKEN not in "\0".join(command)
        assert TOKEN not in "\0".join(f"{key}={value}" for key, value in env.items())
        if "fetch" in command:
            helper = Path(env["GIT_ASKPASS"])
            token_file = helper.with_name(f"{helper.name}.token")
            assert stat.S_IMODE(helper.stat().st_mode) == 0o700
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            assert token_file.read_text() == TOKEN
        stdout = f"{HEAD}\n" if "rev-parse" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("ha_syncapp.database_retention_staging.subprocess.run", run)
    repository = prepare_database_history_staging(
        evidence=_evidence(), staging_root=tmp_path / "staging", token=TOKEN
    )
    try:
        fetch = next(command for command, _env in observed if "fetch" in command)
        assert f"+{HEAD}:refs/syncapp/database-retention" in fetch
        assert "refs/heads/main" not in fetch
        assert "refs/heads/candidate" not in fetch
        assert not (repository / ".syncapp-askpass").exists()
        assert not (repository / ".syncapp-askpass.token").exists()
    finally:
        shutil.rmtree(repository)


def test_staging_failure_removes_repository_and_sanitizes_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command, *, cwd, **kwargs):
        if "fetch" in command:
            raise OSError(TOKEN)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("ha_syncapp.database_retention_staging.subprocess.run", run)
    staging = tmp_path / "staging"
    with pytest.raises(DatabaseRetentionStagingError) as caught:
        prepare_database_history_staging(evidence=_evidence(), staging_root=staging, token=TOKEN)
    assert TOKEN not in str(caught.value)
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize("token", ["", "space token", "line\nbreak", "x" * 513])
def test_invalid_token_is_rejected_before_staging(tmp_path: Path, token: str) -> None:
    staging = tmp_path / "staging"
    with pytest.raises(DatabaseRetentionStagingError, match="authentication"):
        prepare_database_history_staging(evidence=_evidence(), staging_root=staging, token=token)
    assert not staging.exists()
