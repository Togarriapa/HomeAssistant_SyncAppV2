from __future__ import annotations

import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.github_repo import BranchHead
from ha_syncapp.log_history_evidence import (
    LogHistoryRecord,
    validate_trusted_log_history_evidence,
)
from ha_syncapp.log_retention_staging import (
    LogRetentionStagingError,
    prepare_log_history_staging,
)

TARGET = "Owner/Private-Home"
TOKEN = "github-secret-sentinel"
HEAD = "2" * 40
ROOT = "1" * 40
NOW = datetime(2026, 9, 13, tzinfo=UTC)


def _evidence():
    return validate_trusted_log_history_evidence(
        branch_head=BranchHead(TARGET, 123, "logs", HEAD),
        records=(
            LogHistoryRecord(HEAD, NOW - timedelta(days=1), (ROOT,)),
            LogHistoryRecord(ROOT, NOW - timedelta(days=40), ()),
        ),
        reference_time=NOW,
    )


def test_staging_fetches_exact_logs_head_with_ephemeral_private_authentication(
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

    monkeypatch.setattr("ha_syncapp.log_retention_staging.subprocess.run", run)
    repository = prepare_log_history_staging(
        evidence=_evidence(), staging_root=tmp_path / "staging", token=TOKEN
    )
    try:
        fetch = next(command for command, _env in observed if "fetch" in command)
        assert f"+{HEAD}:refs/syncapp/log-retention" in fetch
        assert "refs/heads/main" not in fetch
        assert "refs/heads/candidate" not in fetch
        assert "refs/heads/database" not in fetch
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

    monkeypatch.setattr("ha_syncapp.log_retention_staging.subprocess.run", run)
    staging = tmp_path / "staging"
    with pytest.raises(LogRetentionStagingError) as caught:
        prepare_log_history_staging(evidence=_evidence(), staging_root=staging, token=TOKEN)
    assert TOKEN not in str(caught.value)
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize("token", ["", "space token", "line\nbreak", "x" * 513])
def test_invalid_token_is_rejected_before_staging(tmp_path: Path, token: str) -> None:
    staging = tmp_path / "staging"
    with pytest.raises(LogRetentionStagingError, match="authentication"):
        prepare_log_history_staging(evidence=_evidence(), staging_root=staging, token=token)
    assert not staging.exists()
