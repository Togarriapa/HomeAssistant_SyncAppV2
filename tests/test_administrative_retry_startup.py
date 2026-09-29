from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp.administrative_retry_request import AdministrativeRetryResult
from ha_syncapp.github_repo import RepoIdentity
from ha_syncapp.state import StateStore

REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"
KIND = "candidate"
KEY = "secret-work-key-sentinel"
NOW = datetime(2026, 9, 29, 2, 0, tzinfo=UTC)


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


def _options(**extra: object) -> dict[str, object]:
    return {
        "administrative_retry_request_id": REQUEST_ID,
        "administrative_retry_work_kind": KIND,
        "administrative_retry_work_key": KEY,
        **extra,
    }


def _block(data_dir: Path) -> None:
    with StateStore(data_dir) as store:
        store.enqueue_work(KIND, KEY, now=NOW - timedelta(minutes=2))
        claimed = store.claim_work_kind(KIND, now=NOW - timedelta(minutes=1))
        assert claimed is not None
        store.fail_work(claimed, transient=False, now=NOW - timedelta(seconds=30))


def _patch_passive_startup(
    monkeypatch: pytest.MonkeyPatch,
    stop: service.Shutdown,
) -> None:
    monkeypatch.setattr(service, "RetriggerServer", FakeRetriggerServer)

    def local(store: StateStore, *args: object) -> None:
        assert store._get_work(KIND, KEY).status == "pending"
        stop.requested = True

    monkeypatch.setattr(service, "_run_startup_local_if_configured", local)


def test_startup_consumes_once_then_logs_content_free_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "options.json").write_text(json.dumps(_options()))
    _block(tmp_path)
    first_stop = service.Shutdown()
    _patch_passive_startup(monkeypatch, first_stop)

    service.run(tmp_path, first_stop)
    first = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert first[0]["event"] == "administrative_retry_completed"
    assert set(first[0]) == {"timestamp", "level", "event"}
    assert KEY not in json.dumps(first)

    second_stop = service.Shutdown()
    _patch_passive_startup(monkeypatch, second_stop)
    service.run(tmp_path, second_stop)
    second = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert second[0]["event"] == "administrative_retry_skipped"
    assert set(second[0]) == {"timestamp", "level", "event"}
    assert KEY not in json.dumps(second)


def test_missing_target_is_rejected_once_without_startup_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "options.json").write_text(json.dumps(_options()))
    stop = service.Shutdown()
    stop.requested = True
    monkeypatch.setattr(service, "RetriggerServer", FakeRetriggerServer)

    service.run(tmp_path, stop)
    first = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    service.run(tmp_path, stop)
    second = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert first[0]["event"] == "administrative_retry_rejected"
    assert first[0]["level"] == "warning"
    assert second[0]["event"] == "administrative_retry_skipped"
    assert KEY not in json.dumps(first + second)


def test_retry_request_precedes_repository_trust_and_normal_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "options.json").write_text(
        json.dumps(_options(repo_b="Owner/Home", github_token="secret-sentinel"))
    )
    order: list[str] = []
    stop = service.Shutdown()
    monkeypatch.setattr(service, "RetriggerServer", FakeRetriggerServer)

    def apply(*args: object, **kwargs: object) -> AdministrativeRetryResult:
        order.append("administrative_retry")
        return AdministrativeRetryResult("retried", False)

    def verify(*args: object, **kwargs: object) -> RepoIdentity:
        order.append("repository_trust")
        return RepoIdentity("Owner/Home", 123)

    def local(*args: object, **kwargs: object) -> None:
        order.append("normal_startup")
        stop.requested = True

    monkeypatch.setattr(service, "apply_administrative_retry_request", apply)
    monkeypatch.setattr(service, "fetch_and_verify_private_repository", verify)
    monkeypatch.setattr(service, "_run_startup_local_if_configured", local)

    service.run(tmp_path, stop)

    assert order == ["administrative_retry", "repository_trust", "normal_startup"]
