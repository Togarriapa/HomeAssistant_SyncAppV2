from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from ha_syncapp import candidate_preflight_service as service_module
from ha_syncapp.candidate_preflight_process import (
    CandidatePreflightAction,
    CandidatePreflightOutcome,
    CandidatePreflightProcessError,
    CandidatePreflightProcessResult,
)
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
TOKEN = "github-secret-sentinel"


def _opened_store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    return (
        tmp_path / "home-assistant",
        tmp_path / "fetch-workspaces",
        tmp_path / "analysis-workspaces",
        tmp_path / "staging",
    )


def _service(store: StateStore, tmp_path: Path) -> service_module.CandidatePreflightService:
    return service_module.CandidatePreflightService(
        store,
        *_paths(tmp_path),
        TARGET,
        TOKEN,
    )


def _process(
    action: str | None = "fetch_stage",
    outcome: str = "advanced",
) -> CandidatePreflightProcessResult:
    return CandidatePreflightProcessResult(
        cast(CandidatePreflightAction | None, action),
        cast(CandidatePreflightOutcome, outcome),
    )


def test_service_waits_a_full_fixed_interval_and_runs_one_bounded_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    roots = _paths(tmp_path)
    expected = _process()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def process(*args: object, **kwargs: object) -> CandidatePreflightProcessResult:
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(service_module, "run_candidate_preflight_process", process)
    service = _service(store, tmp_path)
    try:
        service.start(100.0)
        assert calls == []
        assert service.tick(100.999).due is False
        assert calls == []

        tick = service.tick(101.0)

        assert tick == service_module.CandidatePreflightTickResult(due=True, process=expected)
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == (store, *roots, TARGET, TOKEN)
        assert set(kwargs) == {"deploy_key_ingress", "core_token", "reference_time"}
        assert kwargs["deploy_key_ingress"] is None
        assert kwargs["core_token"] is None
        assert isinstance(kwargs["reference_time"], datetime)
        assert kwargs["reference_time"].tzinfo is UTC
    finally:
        store.__exit__(None, None, None)


def test_service_coalesces_missed_intervals_and_never_catches_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[int] = []

    def process(*args: object, **kwargs: object) -> CandidatePreflightProcessResult:
        del args, kwargs
        calls.append(1)
        return _process("analyze", "advanced")

    monkeypatch.setattr(service_module, "run_candidate_preflight_process", process)
    service = _service(store, tmp_path)
    try:
        service.start(0.0)
        assert service.tick(100.0).due is True
        assert service.tick(100.0).due is False
        assert service.tick(100.999).due is False
        assert service.tick(101.0).due is True
        assert calls == [1, 1]
    finally:
        store.__exit__(None, None, None)


@pytest.mark.parametrize("clock", [True, False, -1.0, float("nan"), float("inf"), -float("inf")])
def test_service_rejects_invalid_clock_values(tmp_path: Path, clock: float) -> None:
    store = _opened_store(tmp_path)
    service = _service(store, tmp_path)
    try:
        with pytest.raises(service_module.CandidatePreflightServiceError, match="clock"):
            service.start(clock)
    finally:
        store.__exit__(None, None, None)


def test_service_rejects_clock_values_outside_float_range(tmp_path: Path) -> None:
    store = _opened_store(tmp_path)
    service = _service(store, tmp_path)
    try:
        with pytest.raises(service_module.CandidatePreflightServiceError, match="clock"):
            service.start(10**10000)
    finally:
        store.__exit__(None, None, None)


def test_service_accepts_finite_nonnegative_monotonic_clock_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(
        service_module,
        "run_candidate_preflight_process",
        lambda *args, **kwargs: calls.append(1) or _process(None, "idle"),
    )
    service = _service(store, tmp_path)
    try:
        service.start(0)
        assert service.tick(0.5).due is False
        assert service.tick(1).due is True
        assert service.tick(2.25).due is True
        assert calls == [1, 1]
    finally:
        store.__exit__(None, None, None)


def test_service_enforces_single_start_monotonic_order_and_stop_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(
        service_module,
        "run_candidate_preflight_process",
        lambda *args, **kwargs: calls.append(1) or _process(),
    )
    service = _service(store, tmp_path)
    try:
        with pytest.raises(service_module.CandidatePreflightServiceError, match="not started"):
            service.tick(0.0)
        with pytest.raises(service_module.CandidatePreflightServiceError, match="not started"):
            service.stop()

        service.start(10.0)
        with pytest.raises(
            service_module.CandidatePreflightServiceError,
            match="already started",
        ):
            service.start(11.0)
        with pytest.raises(service_module.CandidatePreflightServiceError, match="backwards"):
            service.tick(9.999)
        assert calls == []

        service.stop()
        assert calls == []
        with pytest.raises(service_module.CandidatePreflightServiceError, match="not started"):
            service.tick(12.0)
        with pytest.raises(service_module.CandidatePreflightServiceError, match="not started"):
            service.stop()
    finally:
        store.__exit__(None, None, None)


def test_recorded_retry_and_blocked_results_keep_service_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    results = iter(
        (
            _process("fetch_stage", "retry"),
            _process("analyze", "blocked"),
            _process("fetch_stage", "advanced"),
        )
    )
    monkeypatch.setattr(
        service_module,
        "run_candidate_preflight_process",
        lambda *args, **kwargs: next(results),
    )
    service = _service(store, tmp_path)
    try:
        service.start(0.0)
        assert service.tick(1.0).process == _process("fetch_stage", "retry")
        assert service.tick(2.0).process == _process("analyze", "blocked")
        assert service.tick(3.0).process == _process("fetch_stage", "advanced")
    finally:
        store.__exit__(None, None, None)


def test_fatal_processor_error_is_fixed_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    private_path = str(tmp_path / "protected-secret-sentinel")

    def fail(*args: object, **kwargs: object) -> CandidatePreflightProcessResult:
        raise CandidatePreflightProcessError(
            f"{TOKEN} {TARGET} {private_path} candidate-sha-secret-sentinel"
        )

    monkeypatch.setattr(service_module, "run_candidate_preflight_process", fail)
    service = _service(store, tmp_path)
    try:
        service.start(0.0)
        with pytest.raises(
            service_module.CandidatePreflightServiceError,
            match="candidate preflight service tick failed closed",
        ) as exc_info:
            service.tick(1.0)
        message = str(exc_info.value)
        assert TOKEN not in message
        assert TARGET not in message
        assert private_path not in message
        assert "candidate-sha-secret-sentinel" not in message
        assert exc_info.value.__cause__ is None
    finally:
        store.__exit__(None, None, None)


def test_service_rejects_unrecognized_processor_categories_without_echoing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    monkeypatch.setattr(
        service_module,
        "run_candidate_preflight_process",
        lambda *args, **kwargs: _process("secret-action-sentinel", "secret-outcome-sentinel"),
    )
    service = _service(store, tmp_path)
    try:
        service.start(0.0)
        with pytest.raises(
            service_module.CandidatePreflightServiceError,
            match="candidate preflight service result is invalid",
        ) as exc_info:
            service.tick(1.0)
        assert "secret-action-sentinel" not in str(exc_info.value)
        assert "secret-outcome-sentinel" not in str(exc_info.value)
    finally:
        store.__exit__(None, None, None)
