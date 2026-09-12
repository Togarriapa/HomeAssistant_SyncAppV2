from pathlib import Path

import pytest
from ha_syncapp import candidate_detection_service as candidate_service
from ha_syncapp.candidate_detection import (
    CandidateDetectionError,
    CandidateDetectionResult,
    CandidateObservation,
)
from ha_syncapp.state import StateStore

TARGET = "Owner/Home"
TOKEN = "github-secret-sentinel"
SHA = "a" * 40


def _opened_store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def _service(
    store: StateStore, *, interval: float = 60.0
) -> candidate_service.CandidateDetectionService:
    return candidate_service.CandidateDetectionService(
        store,
        TARGET,
        TOKEN,
        interval_seconds=interval,
    )


def _detection(commit_sha: str | None = SHA) -> CandidateDetectionResult:
    return CandidateDetectionResult(
        observation=CandidateObservation(
            target=TARGET,
            repository_id=123,
            branch="candidate",
            commit_sha=commit_sha,
        ),
        work=None,
    )


def test_candidate_service_arms_first_deadline_without_immediate_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        candidate_service,
        "detect_and_enqueue_trusted_candidate",
        lambda *args, **kwargs: calls.append("detect") or _detection(),
    )
    service = _service(store)
    try:
        service.start(100.0)
        assert service.tick(159.999) == candidate_service.CandidateDetectionTickResult(
            due=False,
            detection=None,
        )
        assert calls == []
    finally:
        store.__exit__(None, None, None)


def test_candidate_service_due_tick_observes_once_with_owner_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[tuple[object, ...]] = []
    expected = _detection()

    def detect(state: StateStore, target: str, token: str) -> CandidateDetectionResult:
        calls.append((state, target, token))
        return expected

    monkeypatch.setattr(candidate_service, "detect_and_enqueue_trusted_candidate", detect)
    service = _service(store)
    try:
        service.start(10.0)
        assert service.tick(70.0) == candidate_service.CandidateDetectionTickResult(
            due=True,
            detection=expected,
        )
        assert calls == [(store, TARGET, TOKEN)]
    finally:
        store.__exit__(None, None, None)


def test_candidate_service_coalesces_missed_intervals_without_catchup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        candidate_service,
        "detect_and_enqueue_trusted_candidate",
        lambda *args, **kwargs: calls.append("detect") or _detection(),
    )
    service = _service(store, interval=30.0)
    try:
        service.start(0.0)
        assert service.tick(300.0).due is True
        assert service.tick(300.0).due is False
        assert service.tick(329.999).due is False
        assert service.tick(330.0).due is True
        assert calls == ["detect", "detect"]
    finally:
        store.__exit__(None, None, None)


def test_candidate_service_rejects_invalid_clock_lifecycle_and_configuration(
    tmp_path: Path,
) -> None:
    store = _opened_store(tmp_path)
    service = _service(store)
    try:
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="not started"):
            service.tick(0.0)
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="clock"):
            service.start(float("nan"))
        service.start(10.0)
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="already started"):
            service.start(11.0)
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="backwards"):
            service.tick(9.999)
        service.stop()
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="not started"):
            service.tick(70.0)
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="not started"):
            service.stop()
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="interval"):
            candidate_service.CandidateDetectionService(store, TARGET, TOKEN, interval_seconds=0.0)
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="target"):
            candidate_service.CandidateDetectionService(store, "", TOKEN, interval_seconds=60.0)
        with pytest.raises(candidate_service.CandidateDetectionServiceError, match="credential"):
            candidate_service.CandidateDetectionService(store, TARGET, "", interval_seconds=60.0)
    finally:
        store.__exit__(None, None, None)


def test_candidate_service_wraps_detection_failure_without_leaking_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)

    def fail(*args: object, **kwargs: object) -> CandidateDetectionResult:
        raise CandidateDetectionError("github-secret-sentinel upstream detail")

    monkeypatch.setattr(candidate_service, "detect_and_enqueue_trusted_candidate", fail)
    service = _service(store)
    try:
        service.start(0.0)
        with pytest.raises(
            candidate_service.CandidateDetectionServiceError,
            match="candidate service tick failed closed",
        ) as exc_info:
            service.tick(60.0)
        assert TOKEN not in str(exc_info.value)
    finally:
        store.__exit__(None, None, None)


def test_candidate_service_preserves_detector_absence_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _opened_store(tmp_path)
    expected = _detection(None)
    monkeypatch.setattr(
        candidate_service,
        "detect_and_enqueue_trusted_candidate",
        lambda *args, **kwargs: expected,
    )
    service = _service(store)
    try:
        service.start(0.0)
        assert service.tick(60.0).detection == expected
        service.stop()
    finally:
        store.__exit__(None, None, None)
