from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from ha_syncapp import service_entry
from ha_syncapp.config import Config
from ha_syncapp.github_repo import RepoIdentity, RepositoryVerificationError
from ha_syncapp.state import StateStore

TARGET = "Owner/Private-Home"
REPOSITORY_ID = 123
TOKEN = "test-github-token"


def _store(tmp_path: Path) -> tuple[Path, StateStore]:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    return data, store


def test_scheduler_is_absent_without_repo_configuration(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    try:
        assert service_entry._scheduler_if_configured(store, Config()) is None
    finally:
        store.__exit__(None, None, None)


def test_scheduler_requires_existing_trusted_repo_binding(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    try:
        with pytest.raises(Exception, match="not trusted"):
            service_entry._scheduler_if_configured(
                store,
                Config(repo_b=TARGET, github_token=TOKEN),
            )
    finally:
        store.__exit__(None, None, None)


def test_scheduler_uses_validated_configured_interval_after_trust(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    store.bind_repository(TARGET, REPOSITORY_ID)
    try:
        scheduler = service_entry._scheduler_if_configured(
            store,
            Config(repo_b=TARGET, github_token=TOKEN, retrigger_interval_seconds=30),
        )
        assert scheduler is not None
        assert scheduler.interval_seconds == 30
    finally:
        store.__exit__(None, None, None)


def test_automatic_cycle_skips_only_recorder_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, store = _store(tmp_path)
    home = tmp_path / "homeassistant"
    home.mkdir()
    store.bind_repository(TARGET, REPOSITORY_ID)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        service_entry,
        "fetch_and_verify_private_repository",
        lambda *args, **kwargs: RepoIdentity(TARGET, REPOSITORY_ID),
    )
    monkeypatch.setattr(
        service_entry.app,
        "_work_roots",
        lambda *args, **kwargs: tuple(tmp_path / f"work-{index}" for index in range(11)),
    )

    def cycle(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(service_entry, "run_retrigger_cycle", cycle)
    try:
        outcome = service_entry._run_automatic_retrigger_cycle(
            store,
            Config(repo_b=TARGET, github_token=TOKEN),
            data,
            home_assistant_root=home,
        )
    finally:
        store.__exit__(None, None, None)

    assert outcome == "completed"
    args = cast(tuple[object, ...], captured["args"])
    assert args[0] is store
    assert args[1] == home
    assert args[4] is None
    assert args[-2:] == (TARGET, TOKEN)


def test_automatic_cycle_sanitizes_repository_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, store = _store(tmp_path)
    home = tmp_path / "homeassistant"
    home.mkdir()
    store.bind_repository(TARGET, REPOSITORY_ID)

    def fail(*args: object, **kwargs: object) -> RepoIdentity:
        raise RepositoryVerificationError("secret nested failure")

    monkeypatch.setattr(service_entry, "fetch_and_verify_private_repository", fail)
    try:
        outcome = service_entry._run_automatic_retrigger_cycle(
            store,
            Config(repo_b=TARGET, github_token=TOKEN),
            data,
            home_assistant_root=home,
        )
    finally:
        store.__exit__(None, None, None)

    assert outcome == "repo_b_untrusted"
    assert "secret" not in outcome
