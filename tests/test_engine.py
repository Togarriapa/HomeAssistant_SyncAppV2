from pathlib import Path
from unittest.mock import Mock

from ha_syncapp.engine import Engine
from ha_syncapp.errors import Failure
from ha_syncapp.files import Snapshot
from ha_syncapp.git import BRANCHES, GitRepository
from ha_syncapp.journal import Journal
from ha_syncapp.state import StateStore
from test_git import remote_repo


def setup_engine(tmp_path: Path, journal: Journal) -> tuple[Engine, GitRepository, Path]:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "configuration.yaml").write_bytes(b"default_config:\r\n")
    repo = GitRepository(tmp_path / "stage.git", remote_repo(tmp_path))
    engine = Engine(tmp_path / "syncapp", config, journal, lambda: repo, Mock())
    return engine, repo, config


def test_sync_is_blocked_until_explicit_initialization(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        engine, repo, _ = setup_engine(tmp_path, journal)
        engine.tick(now=1)
        engine.tick(now=10000)
        assert journal.jobs() == []
        assert repo.remote_refs() == {}
        initial = engine.request_initialize()
        engine.tick(now=10001)
        assert journal.get(initial.id).status == "succeeded"
        assert journal.value("initialized") is True
        assert set(repo.remote_refs()) == {f"refs/heads/{b}" for b in BRANCHES}
        assert repo.snapshot(repo.ref("main")).files["configuration.yaml"] == b"default_config:\r\n"


def test_lost_init_acknowledgement_recovers_before_sync(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        engine, repo, config = setup_engine(tmp_path, journal)
        job = engine.request_initialize()
        real = repo.initialize

        def lost_ack(commits: dict[str, str]) -> None:
            real(commits)
            raise Failure("simulated_timeout", retryable=True)

        repo.initialize = lost_ack
        engine.tick(now=1)
        assert journal.value("initialized") is not True
        assert journal.get(job.id).phase == "planned"
        planned = repo.remote_refs()
        (config / "configuration.yaml").write_bytes(b"new local content\n")
        repo.initialize = real
        engine.tick(now=10000)
        assert journal.value("initialized") is True
        assert repo.remote_refs() == planned  # Resume the recorded snapshot, not the live changes.


def test_local_sync_debounces_preserves_bytes_and_rejects_remote_overwrite(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        engine, repo, config = setup_engine(tmp_path, journal)
        engine.request_initialize()
        engine.tick(now=1)
        initial = repo.ref("main")
        (config / ".storage").mkdir()
        (config / ".storage/auth").write_bytes(b"secret\x00\xff\r\n")
        engine.tick(now=100)
        assert repo.ref("main") == initial
        engine.tick(now=200)
        synced = repo.ref("main")
        assert synced != initial
        assert repo.snapshot(synced).files[".storage/auth"] == b"secret\x00\xff\r\n"
        foreign = repo.commit(
            Snapshot({"configuration.yaml": b"foreign"}),
            parent=synced,
            message="external main",
            timestamp=201,
        )
        repo.publish("main", foreign, expected=synced)
        (config / "configuration.yaml").write_bytes(b"keep me")
        engine.tick(now=300)
        engine.tick(now=400)
        assert repo.ref("main") == foreign
        assert (config / "configuration.yaml").read_bytes() == b"keep me"
        assert any(
            j.error == "remote_main_changed" and j.status == "blocked" for j in journal.jobs()
        )


def test_initialization_refuses_unhealthy_home_assistant(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        engine, repo, _ = setup_engine(tmp_path, journal)
        engine.homeassistant.health.side_effect = Failure(
            "home_assistant_unhealthy", retryable=True
        )
        engine.request_initialize()
        engine.tick(now=1)
        assert repo.remote_refs() == {}
        assert journal.value("initialized") is not True


def test_failed_sync_push_replays_saved_commit_after_local_changes(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        engine, repo, config = setup_engine(tmp_path, journal)
        engine.request_initialize()
        engine.tick(now=1)
        (config / "configuration.yaml").write_bytes(b"first change\r\n")
        engine.tick(now=100)
        original = repo.publish

        def lose_ack(*args: object, **kwargs: object) -> None:
            original(*args, **kwargs)
            raise Failure("network_timeout", retryable=True)

        repo.publish = lose_ack
        engine.tick(now=200)
        pushed = repo.ref("main")
        (config / "configuration.yaml").write_bytes(b"later change\n")
        repo.publish = original
        engine.tick(now=300)
        assert journal.value("main") == pushed
        assert repo.snapshot(pushed).files["configuration.yaml"] == b"first change\r\n"
        assert all(j.status == "succeeded" for j in journal.jobs())


def test_hourly_retrigger_scan_does_not_create_work_before_initialization(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        journal = Journal(store.connection)
        engine, _, _ = setup_engine(tmp_path, journal)
        engine.tick(now=1)
        engine.tick(now=3599)
        assert [e["event"] for e in journal.events()] == ["retrigger_skipped"]
        engine.tick(now=3601)
        assert len(journal.events()) == 2
        assert journal.jobs() == []
