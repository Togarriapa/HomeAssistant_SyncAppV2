import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from ha_syncapp.__main__ import Shutdown, run
from ha_syncapp.github_repo import RepoIdentity, RepositoryVerificationError
from ha_syncapp.local_startup import LocalStartupResult
from ha_syncapp.runtime_startup import RuntimeStartupResult

SOURCE = Path(__file__).resolve().parents[1] / "syncapp/src"


def start_service(data_dir: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "ha_syncapp", "--data-dir", str(data_dir)],
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def wait_for_start(process: subprocess.Popen[str]) -> dict:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], 10)
    assert ready, "service did not emit its startup event"
    event = json.loads(process.stdout.readline())
    assert event["event"] == "service_started", event
    return event


def stop(process: subprocess.Popen[str], sig: int = signal.SIGTERM) -> tuple[str, str]:
    process.send_signal(sig)
    try:
        return process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=10)
        raise


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_service_stops_cleanly_and_preserves_identity(tmp_path: Path, sig: int) -> None:
    (tmp_path / "options.json").write_text("{}")
    first = start_service(tmp_path)
    try:
        started = wait_for_start(first)
        assert started["mode"] == "passive"
        output, errors = stop(first, sig)
        assert first.returncode == 0
        assert errors == ""
        assert json.loads(output)["event"] == "service_stopped"
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate()
    second = start_service(tmp_path)
    try:
        restarted = wait_for_start(second)
        assert restarted["installation_id"] == started["installation_id"]
        assert restarted["boot_count"] == 2
        assert restarted["interrupted_run_id"] is None
    finally:
        stop(second)


def test_kernel_releases_lock_after_kill_and_interruption_is_detected(tmp_path: Path) -> None:
    (tmp_path / "options.json").write_text("{}")
    first = start_service(tmp_path)
    try:
        started = wait_for_start(first)
        competitor = start_service(tmp_path)
        output, errors = competitor.communicate(timeout=10)
        assert competitor.returncode != 0
        assert errors == ""
        assert json.loads(output)["reason"] == "already_running"
    finally:
        first.kill()
        first.communicate(timeout=10)
    recovered = start_service(tmp_path)
    try:
        restarted = wait_for_start(recovered)
        assert restarted["interrupted_run_id"] == started["run_id"]
        assert restarted["boot_count"] == 2
    finally:
        stop(recovered)


def test_invalid_options_do_not_create_state_or_disclose_input(tmp_path: Path) -> None:
    (tmp_path / "options.json").write_text('{"github_token":"secret-sentinel"}')
    process = start_service(tmp_path)
    output, errors = process.communicate(timeout=10)
    assert process.returncode != 0
    assert errors == ""
    assert "secret-sentinel" not in output
    assert json.loads(output)["reason"] == "configuration_invalid"
    assert not (tmp_path / "syncapp").exists()


def test_configured_repo_is_verified_and_bound_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "secret-sentinel"
    (tmp_path / "options.json").write_text(
        json.dumps({"repo_b": "Owner/Home", "github_token": token})
    )
    calls: list[tuple[str, str, int | None]] = []

    def verify(target: str, supplied_token: str, *, expected_id: int | None = None) -> RepoIdentity:
        calls.append((target, supplied_token, expected_id))
        return RepoIdentity(target="Owner/Home", repository_id=123)

    monkeypatch.setattr("ha_syncapp.__main__.fetch_and_verify_private_repository", verify)
    stop_now = Shutdown()
    stop_now.requested = True
    run(tmp_path, stop_now)
    first_output = capsys.readouterr().out
    assert token not in first_output
    assert calls == [("Owner/Home", token, None)]

    run(tmp_path, stop_now)
    second_output = capsys.readouterr().out
    assert token not in second_output
    assert calls[-1] == ("Owner/Home", token, 123)


def test_normal_bootstraps_occur_only_after_repository_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "github-secret-sentinel"
    (tmp_path / "options.json").write_text(
        json.dumps({"repo_b": "Owner/Home", "github_token": token})
    )
    order: list[str] = []
    stop_after_bootstrap = Shutdown()

    def verify(target: str, supplied_token: str, *, expected_id: int | None = None) -> RepoIdentity:
        assert target == "Owner/Home"
        assert supplied_token == token
        assert expected_id is None
        order.append("trust")
        return RepoIdentity(target=target, repository_id=123)

    def local_bootstrap(store, config, data_dir: Path) -> LocalStartupResult | None:
        assert config.repo_b == "Owner/Home"
        assert config.github_token == token
        assert data_dir == tmp_path
        assert store.repository_id("Owner/Home") == 123
        order.append("local_bootstrap")
        return None

    def runtime_bootstrap(store, config, data_dir: Path) -> RuntimeStartupResult | None:
        assert config.repo_b == "Owner/Home"
        assert config.github_token == token
        assert data_dir == tmp_path
        assert store.repository_id("Owner/Home") == 123
        order.append("runtime_bootstrap")
        stop_after_bootstrap.requested = True
        return None

    monkeypatch.setattr("ha_syncapp.__main__.fetch_and_verify_private_repository", verify)
    monkeypatch.setattr("ha_syncapp.__main__._run_startup_local_if_configured", local_bootstrap)
    monkeypatch.setattr("ha_syncapp.__main__._run_startup_runtime_if_configured", runtime_bootstrap)

    run(tmp_path, stop_after_bootstrap)
    output = capsys.readouterr().out

    assert order == ["trust", "local_bootstrap", "runtime_bootstrap"]
    assert token not in output


def test_shutdown_after_local_bootstrap_skips_later_normal_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "github-secret-sentinel"
    (tmp_path / "options.json").write_text(
        json.dumps({"repo_b": "Owner/Home", "github_token": token})
    )
    stop_after_local = Shutdown()

    monkeypatch.setattr(
        "ha_syncapp.__main__.fetch_and_verify_private_repository",
        lambda target, supplied_token, expected_id=None: RepoIdentity(
            target=target, repository_id=123
        ),
    )

    def local_bootstrap(store, config, data_dir: Path) -> None:
        stop_after_local.requested = True

    def forbidden_runtime(*args: object) -> None:
        pytest.fail("shutdown after Local bootstrap must skip runtime bootstrap")

    monkeypatch.setattr("ha_syncapp.__main__._run_startup_local_if_configured", local_bootstrap)
    monkeypatch.setattr(
        "ha_syncapp.__main__._run_startup_runtime_if_configured", forbidden_runtime
    )

    run(tmp_path, stop_after_local)


def test_repo_verification_failure_happens_before_run_is_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "options.json").write_text(
        json.dumps({"repo_b": "Owner/Home", "github_token": "secret-sentinel"})
    )

    def fail(target: str, token: str, *, expected_id: int | None = None) -> RepoIdentity:
        raise RepositoryVerificationError("sanitized")

    monkeypatch.setattr("ha_syncapp.__main__.fetch_and_verify_private_repository", fail)
    with pytest.raises(RepositoryVerificationError):
        run(tmp_path, Shutdown())
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        boot_count, active_run_id = db.execute(
            "SELECT boot_count, active_run_id FROM installation WHERE singleton = 1"
        ).fetchone()
    assert boot_count == 0
    assert active_run_id is None
