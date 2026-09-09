import json
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

import pytest

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
