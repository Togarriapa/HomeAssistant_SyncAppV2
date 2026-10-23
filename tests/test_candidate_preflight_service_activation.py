from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp.candidate_detection_service import CandidateDetectionServiceError
from ha_syncapp.candidate_deploy_key_ingress import DeployKeyCandidateIngress
from ha_syncapp.candidate_preflight_process import CandidatePreflightProcessResult
from ha_syncapp.candidate_preflight_service import (
    CandidatePreflightServiceError,
    CandidatePreflightTickResult,
)
from ha_syncapp.config import Config
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
TOKEN = "github-secret-sentinel"
CORE_TOKEN = "supervisor-token-sentinel"


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


class FakeDetectionService:
    def __init__(
        self,
        order: list[str],
        *,
        stop: service.Shutdown,
        stop_on_tick: bool = False,
    ) -> None:
        self._order = order
        self._stop = stop
        self._stop_on_tick = stop_on_tick

    def start(self, now: float) -> None:
        assert now >= 0
        self._order.append("detection_start")

    def tick(self, now: float) -> None:
        assert now >= 0
        self._order.append("detection_tick")
        if self._stop_on_tick:
            self._stop.requested = True

    def stop(self) -> None:
        self._order.append("detection_stop")


class FakePreflightService:
    def __init__(
        self,
        order: list[str],
        stop: service.Shutdown,
        *,
        fail_tick: bool = False,
        result: CandidatePreflightProcessResult | None = None,
    ) -> None:
        self._order = order
        self._stop = stop
        self._fail_tick = fail_tick
        self._result = result or CandidatePreflightProcessResult("fetch_stage", "advanced")

    def start(self, now: float) -> None:
        assert now >= 0
        self._order.append("preflight_start")

    def tick(self, now: float) -> CandidatePreflightTickResult:
        assert now >= 0
        self._order.append("preflight_tick")
        if self._fail_tick:
            raise CandidatePreflightServiceError("secret preflight failure sentinel")
        self._stop.requested = True
        return CandidatePreflightTickResult(due=True, process=self._result)

    def stop(self) -> None:
        self._order.append("preflight_stop")


def _write_config(data_dir: Path) -> None:
    (data_dir / "options.json").write_text(json.dumps({"repo_b": TARGET, "github_token": TOKEN}))


def _patch_common(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    def verify(
        target: str,
        token: str,
        *,
        expected_id: int | None = None,
    ) -> RepoIdentity:
        assert (target, token, expected_id) == (TARGET, TOKEN, None)
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


def test_candidate_preflight_starts_after_detection_and_ticks_after_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    detection = FakeDetectionService(order, stop=stop)
    preflight = FakePreflightService(order, stop)

    def build_detection(
        store: StateStore,
        config: Config,
    ) -> FakeDetectionService:
        assert store.repository_id(TARGET) == 123
        assert config.github_token == TOKEN
        order.append("detection_build")
        return detection

    def build_preflight(
        store: StateStore,
        config: Config,
        data_dir: Path,
        deploy_key_ingress: DeployKeyCandidateIngress | None = None,
    ) -> FakePreflightService:
        assert store.repository_id(TARGET) == 123
        assert config.github_token == TOKEN
        assert data_dir == tmp_path
        assert deploy_key_ingress is None
        order.append("preflight_build")
        return preflight

    monkeypatch.setattr(service, "_candidate_detection_service_if_configured", build_detection)
    monkeypatch.setattr(service, "_candidate_preflight_service_if_configured", build_preflight)
    monkeypatch.setenv("SUPERVISOR_TOKEN", CORE_TOKEN)

    service.run(tmp_path, stop)

    assert order == [
        "trust",
        "detection_build",
        "preflight_build",
        "detection_start",
        "preflight_start",
        "detection_tick",
        "preflight_tick",
        "preflight_stop",
        "detection_stop",
    ]
    output = capsys.readouterr().out
    assert TOKEN not in output and CORE_TOKEN not in output
    assert '"mode":"active"' in output
    assert (
        '"event":"candidate_preflight_processed","action":"fetch_stage","outcome":"advanced"'
        in output
    )
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        assert (
            database.execute(
                "SELECT active_run_id FROM installation WHERE singleton = 1"
            ).fetchone()[0]
            is None
        )


def test_preflight_exception_stops_started_services_and_preserves_interrupted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    detection = FakeDetectionService(order, stop=stop)
    preflight = FakePreflightService(order, stop, fail_tick=True)
    monkeypatch.setattr(
        service,
        "_candidate_detection_service_if_configured",
        lambda *args: detection,
    )
    monkeypatch.setattr(
        service,
        "_candidate_preflight_service_if_configured",
        lambda *args: preflight,
    )

    with pytest.raises(CandidatePreflightServiceError, match="secret preflight failure"):
        service.run(tmp_path, stop)

    assert order[-5:] == [
        "detection_start",
        "preflight_start",
        "detection_tick",
        "preflight_tick",
        "preflight_stop",
    ] or order[-6:] == [
        "detection_start",
        "preflight_start",
        "detection_tick",
        "preflight_tick",
        "preflight_stop",
        "detection_stop",
    ]
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as database:
        assert (
            database.execute(
                "SELECT active_run_id FROM installation WHERE singleton = 1"
            ).fetchone()[0]
            is not None
        )


def test_preflight_builder_uses_trusted_identity_and_private_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    home = tmp_path / "homeassistant"
    home.mkdir(mode=0o700)
    config = Config(repo_b=TARGET, github_token=TOKEN)
    monkeypatch.setenv("SUPERVISOR_TOKEN", CORE_TOKEN)
    with StateStore(data_dir) as store:
        store.bind_repository(TARGET, 123)
        built = service._candidate_preflight_service_if_configured(
            store,
            config,
            data_dir,
            home_assistant_root=home,
        )

        assert built is not None
        assert built._target == TARGET
        assert built._github_token == TOKEN
        assert built._core_token == CORE_TOKEN
        protected = (data_dir / "syncapp").resolve(strict=True)
        roots = (
            built._fetch_workspace_root,
            built._analysis_workspace_root,
            built._staging_root,
        )
        assert all(protected in root.parents for root in roots)
        assert all(home not in root.parents and root not in home.parents for root in roots)
        assert all(root.is_dir() and root.stat().st_mode & 0o777 == 0o700 for root in roots)


def test_unconfigured_or_untrusted_candidate_preflight_is_disabled_or_fails_closed(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    home = tmp_path / "homeassistant"
    home.mkdir()
    with StateStore(data_dir) as store:
        assert (
            service._candidate_preflight_service_if_configured(
                store,
                Config(),
                data_dir,
                home_assistant_root=home,
            )
            is None
        )
        with pytest.raises(CandidatePreflightServiceError, match="not trusted"):
            service._candidate_preflight_service_if_configured(
                store,
                Config(repo_b=TARGET, github_token=TOKEN),
                data_dir,
                home_assistant_root=home,
            )


def test_main_uses_fixed_preflight_failure_category(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        service,
        "run",
        lambda *_args: (_ for _ in ()).throw(
            CandidatePreflightServiceError(f"{TOKEN} {CORE_TOKEN} secret path")
        ),
    )
    monkeypatch.setattr("sys.argv", ["syncapp", "--data-dir", "/tmp"])

    assert service.main() == 16
    output = capsys.readouterr().out
    assert '"reason":"candidate_preflight_service_failed"' in output
    assert TOKEN not in output and CORE_TOKEN not in output and "secret path" not in output


def test_preflight_event_is_absent_for_idle_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    detection = FakeDetectionService(order, stop=stop)
    preflight = FakePreflightService(
        order,
        stop,
        result=CandidatePreflightProcessResult(None, "idle"),
    )
    monkeypatch.setattr(
        service,
        "_candidate_detection_service_if_configured",
        lambda *args: detection,
    )
    monkeypatch.setattr(
        service,
        "_candidate_preflight_service_if_configured",
        lambda *args: preflight,
    )

    service.run(tmp_path, stop)

    output = capsys.readouterr().out
    assert '"event":"candidate_preflight_processed"' not in output
    assert TOKEN not in output and CORE_TOKEN not in output


def test_shutdown_from_detection_tick_skips_preflight_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)
    detection = FakeDetectionService(order, stop=stop, stop_on_tick=True)
    preflight = FakePreflightService(order, stop)
    monkeypatch.setattr(
        service,
        "_candidate_detection_service_if_configured",
        lambda *args: detection,
    )
    monkeypatch.setattr(
        service,
        "_candidate_preflight_service_if_configured",
        lambda *args: preflight,
    )

    service.run(tmp_path, stop)

    assert "detection_tick" in order
    assert "preflight_tick" not in order
    assert "preflight_stop" in order
    assert "detection_stop" in order
    assert '"event":"candidate_preflight_processed"' not in capsys.readouterr().out


def test_shutdown_during_detection_construction_skips_preflight_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_config(tmp_path)
    stop = service.Shutdown()
    order: list[str] = []
    _patch_common(monkeypatch, order)

    def build_detection(*args: object) -> FakeDetectionService:
        order.append("detection_build")
        stop.requested = True
        return FakeDetectionService(order, stop=stop)

    def build_preflight(*args: object) -> FakePreflightService:
        order.append("preflight_build")
        return FakePreflightService(order, stop)

    monkeypatch.setattr(service, "_candidate_detection_service_if_configured", build_detection)
    monkeypatch.setattr(service, "_candidate_preflight_service_if_configured", build_preflight)

    service.run(tmp_path, stop)

    assert "detection_build" in order
    assert "preflight_build" not in order
