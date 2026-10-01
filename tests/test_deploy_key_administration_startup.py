from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from ha_syncapp import __main__ as service
from ha_syncapp.deploy_key_administration import DeployKeyAdministrativeResult
from ha_syncapp.github_repo import RepoIdentity

REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"


class FakeRetriggerServer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> FakeRetriggerServer:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def serve_once(self, handler: Callable[[object], str], *, timeout_seconds: float) -> None:
        del handler, timeout_seconds


def _options(action: str = "generate") -> dict[str, object]:
    return {
        "repo_b": "Owner/Home",
        "github_token": "secret-sentinel",
        "repo_b_admin_action": action,
        "repo_b_admin_request_id": REQUEST_ID,
    }


def test_startup_runs_operator_action_after_identity_pin_and_logs_safe_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "options.json").write_text(json.dumps(_options()))
    stop = service.Shutdown()
    order: list[str] = []

    def verify(*args: object, **kwargs: object) -> RepoIdentity:
        order.append("repository_trust")
        return RepoIdentity("Owner/Home", 123)

    def apply(*args: object, **kwargs: object) -> DeployKeyAdministrativeResult:
        order.append("operator_action")
        stop.requested = True
        return DeployKeyAdministrativeResult(
            "generated", False, "ssh-ed25519 SAFE public", "SHA256:" + "A" * 43, REQUEST_ID
        )

    monkeypatch.setattr(service, "fetch_and_verify_private_repository", verify)
    monkeypatch.setattr(service, "apply_deploy_key_administrative_request", apply)
    monkeypatch.setattr(service, "RetriggerServer", FakeRetriggerServer)

    service.run(tmp_path, stop)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert order == ["repository_trust", "operator_action"]
    assert events[0] == {
        "timestamp": events[0]["timestamp"],
        "level": "info",
        "event": "repo_b_admin_completed",
        "action": "generate",
        "outcome": "generated",
        "public_key": "ssh-ed25519 SAFE public",
        "fingerprint": "SHA256:" + "A" * 43,
        "generation_id": REQUEST_ID,
    }
    assert "secret-sentinel" not in json.dumps(events)


def test_startup_logs_replay_without_repeating_result_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "options.json").write_text(json.dumps(_options("test")))
    stop = service.Shutdown()
    monkeypatch.setattr(
        service,
        "fetch_and_verify_private_repository",
        lambda *args, **kwargs: RepoIdentity("Owner/Home", 123),
    )

    def apply(*args: object, **kwargs: object) -> DeployKeyAdministrativeResult:
        stop.requested = True
        return DeployKeyAdministrativeResult("tested", True, None, "SHA256:" + "A" * 43, REQUEST_ID)

    monkeypatch.setattr(service, "apply_deploy_key_administrative_request", apply)
    monkeypatch.setattr(service, "RetriggerServer", FakeRetriggerServer)

    service.run(tmp_path, stop)

    event = json.loads(capsys.readouterr().out.splitlines()[0])
    assert event == {
        "timestamp": event["timestamp"],
        "level": "info",
        "event": "repo_b_admin_skipped",
        "action": "test",
        "outcome": "tested",
    }
