from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from ha_syncapp.log_history_replace_transport import (
    build_log_history_replacement,
    replace_logs_history,
)
from ha_syncapp.log_history_replacement import LogHistoryReplacementAuthorization

TOKEN = "github-secret-sentinel"


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
    for index in range(2):
        (repository / "logs.jsonl").write_text(f"entry-{index}\n", encoding="utf-8")
        _git(repository, "add", "logs.jsonl")
        _git(repository, "commit", "-m", f"logs-{index}")
        shas.append(_git(repository, "rev-parse", "HEAD"))
    authorization = LogHistoryReplacementAuthorization(
        target="owner/private-repo",
        repository_id=123,
        branch="logs",
        expected_head_sha=shas[1],
        retained_shas=(shas[1],),
        pruned_shas=(shas[0],),
    )
    artifact = build_log_history_replacement(
        authorization=authorization,
        repository=repository,
    )
    return repository, authorization, artifact


def test_private_publication_uses_ephemeral_askpass_without_token_in_argv(tmp_path: Path) -> None:
    repository, authorization, artifact = _replacement(tmp_path)
    credential_paths: list[tuple[Path, Path]] = []

    def runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if "push" not in command:
            return subprocess.run(  # nosec B603
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=timeout,
            )
        assert TOKEN not in " ".join(command)
        askpass_values = [value for value in command if value.startswith("core.askPass=")]
        assert len(askpass_values) == 1
        askpass = Path(askpass_values[0].split("=", 1)[1])
        token_file = askpass.with_suffix(".token")
        assert askpass.parent == repository
        assert stat.S_IMODE(askpass.stat().st_mode) == 0o700
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert token_file.read_text(encoding="utf-8") == TOKEN
        credential_paths.append((askpass, token_file))
        return subprocess.CompletedProcess(command, 0, "", "")

    assert replace_logs_history(
        authorization=authorization,
        artifact=artifact,
        token=TOKEN,
        runner=runner,
    )
    assert len(credential_paths) == 1
    askpass, token_file = credential_paths[0]
    assert not askpass.exists()
    assert not token_file.exists()
