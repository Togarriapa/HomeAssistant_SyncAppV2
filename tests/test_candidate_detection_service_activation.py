from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp.candidate_detection_service import CandidateDetectionServiceError
from ha_syncapp.config import Config
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
TOKEN = "github-secret-sentinel"


class FakeRetriggerServer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> FakeRetriggerServer:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def serve_once(
        self,
        handler: Callable[[object], str],
        *,
        timeout_seconds: float,
    ) -> None:
        del handler
        assert timeout_seconds == 0.25


class FakeCandidateService:
    def __init__(
        self,
        order: list[str],
        stop: service.Shutdown,
        *,
        fail_tick: bool = False,
    ) -> None:
        self._order = order
        self._stop = stop
        self._fail_tick = fail_tick

    def start(self, now: float) -> None:
        assert now >= 0
        self._order.append("candidate_start")

    def tick(self, now: float) -> None:
        assert now >= 0
        self._order.append("candidate_tick")
        if self._fail_tick:
            raise CandidateDetectionServiceError("secret candidate failure")
        self._stop.requested = True

    def stop(self) -> None:
        self._order.append("candidate_stop")


def _write_config(data_dir: Path) -> None:
    (data_dir / "options.json").write_text(json.dumps({"repo_b": TARGET, "github_token": TOKEN}))


def _patch_common(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    def verify(
        target: str,
        token: str,
        *,
        expected_id: int | None = None,
    ) -> RepoIdentity:
        assert target == TARGET
        assert token == TOKEN
        assert expected_id is None
        order.append("trust")
        return RepoIdentity(target=TARGET, repository_id=123)

    monkeypatch.setattr(service, "fetch_and_verify_private_repository", verify)
    monkeypatch.setattr(service, "RetriggerServer", FakeRetriggerServer)
    monkeypatch.setattr(service, "_run_startup_local_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_local_change_service_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_run_startup_database_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_database_sync_service_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_run_startup_runtime_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_runtime_event_bridge_if_configured", lambda *args: None)
    monkeypatch.setattr(service, "_log_sync_service_if_configured", lambda *args: None)


def test_configured_service_runs_candidate_detection_after_trusted_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    candidate = FakeCandidateService(order, stop)

    def build_candidate(
        store: StateStore,
        config: Config,
    ) -> FakeCandidateService:
        assert store.repository_id(TARGET) == 123
        assert config.github_token == TOKEN
        order.append("candidate_build")
        return candidate

    monkeypatch.setattr(
        service,
        "_candidate_detection_service_if_configured",
        build_candidate,
    )

    service.run(tmp_path, stop)

    assert order == [
        "trust",
        "candidate_build",
        "candidate_start",
        "candidate_tick",
        "candidate_stop",
    ]
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert '"mode":"active"' in output
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        assert (
            database.execute(
                "SELECT active_run_id FROM installation WHERE singleton = 1"
            ).fetchone()[0]
            is None
        )


def test_shutdown_before_candidate_construction_skips_detector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)

    def stop_after_logs(*args: object) -> None:
        order.append("logs_build")
        stop.requested = True

    monkeypatch.setattr(service, "_log_sync_service_if_configured", stop_after_logs)
    monkeypatch.setattr(
        service,
        "_candidate_detection_service_if_configured",
        lambda *args: pytest.fail("shutdown built candidate detector"),
    )

    service.run(tmp_path, stop)

    assert order == ["trust", "logs_build"]


def test_candidate_detection_failure_preserves_interrupted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    monkeypatch.setattr(
        service,
        "_candidate_detection_service_if_configured",
        lambda *args: FakeCandidateService(order, stop, fail_tick=True),
    )

    with pytest.raises(CandidateDetectionServiceError, match="secret candidate failure"):
        service.run(tmp_path, stop)

    assert order[-3:] == ["candidate_start", "candidate_tick", "candidate_stop"]
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        assert (
            database.execute(
                "SELECT active_run_id FROM installation WHERE singleton = 1"
            ).fetchone()[0]
            is not None
        )


def test_unconfigured_candidate_detector_is_disabled(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    with StateStore(data) as store:
        assert service._candidate_detection_service_if_configured(store, Config()) is None


def test_configured_candidate_detector_uses_trusted_identity_and_minute_cadence(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = Config(repo_b=TARGET, github_token=TOKEN)
    with StateStore(data) as store:
        store.bind_repository(TARGET, 123)
        configured = service._candidate_detection_service_if_configured(store, config)

        assert configured is not None
        assert configured._target == TARGET
        assert configured._github_token == TOKEN
        assert configured._interval_seconds == 60.0


def test_configured_candidate_detector_rejects_missing_trust(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = Config(repo_b=TARGET, github_token=TOKEN)
    with (
        StateStore(data) as store,
        pytest.raises(CandidateDetectionServiceError, match="not trusted"),
    ):
        service._candidate_detection_service_if_configured(store, config)
