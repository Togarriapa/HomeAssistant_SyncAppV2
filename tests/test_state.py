import os
import sqlite3
import stat
from pathlib import Path
from uuid import UUID

import pytest
from ha_syncapp.state import AlreadyRunning, StateError, StateStore


def test_schema_four_preserves_existing_work_binding_and_baseline(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        before = store.start_run()
        store.bind_repository("Owner/Home", 123)
        store.enqueue_work("runtime", "queued-before-upgrade")
        store.record_synchronization_baseline("Owner/Home", "main", "a" * 64, "b" * 40)
    with sqlite3.connect(tmp_path / "syncapp/state.sqlite3") as db:
        for name in ("values_store", "jobs", "events"):
            db.execute(f"DROP TABLE {name}")
        db.execute("PRAGMA user_version = 4")
    with StateStore(tmp_path) as store:
        after = store.start_run()
        assert after.installation_id == before.installation_id
        assert after.interrupted_run_id == before.run_id
        assert store.repository_id("Owner/Home") == 123
        assert store.claim_work().work_key == "queued-before-upgrade"
        assert store.synchronization_baseline("Owner/Home", "main").commit_sha == "b" * 40
        assert store.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_foundation_schema_migrates_without_losing_identity_or_interrupted_run(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path) as store:
        before = store.start_run()
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE values_store")
        db.execute("DROP TABLE jobs")
        db.execute("DROP TABLE events")
        db.execute("DROP TABLE work")
        db.execute("DROP TABLE repository_binding")
        db.execute("DROP TABLE synchronization_baseline")
        db.execute("PRAGMA user_version = 1")
    with StateStore(tmp_path) as store:
        after = store.start_run()
        assert after.installation_id == before.installation_id
        assert after.interrupted_run_id == before.run_id
        assert after.boot_count == 2
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 5
        store.finish_run()


def test_identity_and_clean_shutdown_survive_reopening(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        first = store.start_run()
        assert UUID(first.installation_id)
        assert UUID(first.run_id)
        assert first.boot_count == 1
        assert first.interrupted_run_id is None
        store.finish_run()
    with StateStore(tmp_path) as store:
        second = store.start_run()
        assert second.installation_id == first.installation_id
        assert second.run_id != first.run_id
        assert second.boot_count == 2
        assert second.interrupted_run_id is None
        store.finish_run()


def test_interrupted_run_is_reported_without_resetting_identity(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        first = store.start_run()
    with StateStore(tmp_path) as store:
        second = store.start_run()
        assert second.interrupted_run_id == first.run_id
        assert second.installation_id == first.installation_id
        assert second.boot_count == 2


def test_two_instances_cannot_open_same_state(tmp_path: Path) -> None:
    with StateStore(tmp_path) as first:
        first.start_run()
        with pytest.raises(AlreadyRunning), StateStore(tmp_path):
            pytest.fail("second instance acquired the lock")
    with StateStore(tmp_path) as next_store:
        assert next_store.start_run().boot_count == 2


def test_lifecycle_misuse_is_rejected(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        with pytest.raises(StateError):
            store.finish_run()
        store.start_run()
        with pytest.raises(StateError):
            store.start_run()
        store.finish_run()
        with pytest.raises(StateError):
            store.finish_run()


def test_state_and_lock_permissions_are_private(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.start_run()
        root = tmp_path / "syncapp"
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for path in root.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("version", [0, 5, 999])
def test_unrecognized_database_is_preserved(tmp_path: Path, version: int) -> None:
    root = tmp_path / "syncapp"
    root.mkdir()
    path = root / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE future_data (value TEXT)")
        db.execute("INSERT INTO future_data VALUES ('preserve-me')")
        db.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    with pytest.raises(StateError), StateStore(tmp_path):
        pytest.fail("unrecognized state was accepted")
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", [b"secret-sentinel: not a database", b""])
def test_corrupt_database_is_not_recreated(tmp_path: Path, content: bytes) -> None:
    root = tmp_path / "syncapp"
    root.mkdir()
    path = root / "state.sqlite3"
    path.write_bytes(content)
    before = path.read_bytes()
    with pytest.raises(StateError) as error, StateStore(tmp_path):
        pytest.fail("corrupt state was accepted")
    assert "secret-sentinel" not in str(error.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["instance.lock", "state.sqlite3", "state.sqlite3-journal"])
def test_symlinked_state_files_are_rejected(tmp_path: Path, name: str) -> None:
    root = tmp_path / "syncapp"
    root.mkdir()
    target = tmp_path / "preserve.txt"
    target.write_bytes(b"preserve-me")
    (root / name).symlink_to(target)
    with pytest.raises(StateError), StateStore(tmp_path):
        pytest.fail("symlink was accepted")
    assert target.read_bytes() == b"preserve-me"


def test_symlinked_state_directory_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    (tmp_path / "syncapp").symlink_to(target, target_is_directory=True)
    with pytest.raises(StateError), StateStore(tmp_path):
        pytest.fail("symlink was accepted")
    assert not list(target.iterdir())


def test_hardlinked_state_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "syncapp"
    root.mkdir()
    target = tmp_path / "preserve.txt"
    target.write_bytes(b"preserve-me")
    os.link(target, root / "state.sqlite3")
    with pytest.raises(StateError), StateStore(tmp_path):
        pytest.fail("hardlink was accepted")
    assert target.read_bytes() == b"preserve-me"


def test_missing_identity_in_existing_schema_is_not_reset(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.start_run()
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM installation")
    before = path.read_bytes()
    with pytest.raises(StateError), StateStore(tmp_path):
        pytest.fail("missing identity was recreated")
    assert path.read_bytes() == before


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_orphaned_sidecars_do_not_create_a_new_identity(tmp_path: Path, suffix: str) -> None:
    root = tmp_path / "syncapp"
    root.mkdir()
    sidecar = root / f"state.sqlite3{suffix}"
    sidecar.write_bytes(b"preserve-recovery-data")
    with pytest.raises(StateError), StateStore(tmp_path):
        pytest.fail("orphaned recovery data was ignored")
    assert not (root / "state.sqlite3").exists()
    assert sidecar.read_bytes() == b"preserve-recovery-data"


def test_failed_start_transaction_preserves_previous_run(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        first = store.start_run()
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER fail_start BEFORE UPDATE ON installation "
            "BEGIN SELECT RAISE(ABORT, 'secret-sentinel'); END"
        )
    with StateStore(tmp_path) as store, pytest.raises(StateError) as error:
        store.start_run()
    assert "secret-sentinel" not in str(error.value)
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER fail_start")
    with StateStore(tmp_path) as store:
        second = store.start_run()
        assert second.boot_count == 2
        assert second.installation_id == first.installation_id
        assert second.interrupted_run_id == first.run_id


def test_repository_binding_is_stable_and_idempotent(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        assert store.repository_id("Owner/Home") is None
        store.bind_repository("Owner/Home", 12345)
        store.bind_repository("Owner/Home", 12345)
        assert store.repository_id("Owner/Home") == 12345
    with StateStore(tmp_path) as store:
        assert store.repository_id("Owner/Home") == 12345
        with pytest.raises(StateError):
            store.bind_repository("Owner/Home", 99999)
        assert store.repository_id("Owner/Home") == 12345


def test_distinct_repository_target_can_have_its_own_binding(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/First", 1)
        store.bind_repository("Owner/Second", 2)
        assert store.repository_id("Owner/First") == 1
        assert store.repository_id("Owner/Second") == 2


@pytest.mark.parametrize(
    ("target", "repository_id"),
    [("", 1), ("x\nsecret-sentinel", 1), ("Owner/Home", 0), ("Owner/Home", True)],
)
def test_invalid_repository_binding_fails_without_disclosure(
    tmp_path: Path, target: str, repository_id: object
) -> None:
    with StateStore(tmp_path) as store, pytest.raises(StateError) as error:
        store.bind_repository(target, repository_id)  # type: ignore[arg-type]
    assert "secret-sentinel" not in str(error.value)


def test_schema_v2_migrates_repository_binding_without_losing_work(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.enqueue_work("runtime", "existing", now=None)
    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE repository_binding")
        db.execute("DROP TABLE synchronization_baseline")
        db.execute("DROP TABLE values_store")
        db.execute("DROP TABLE jobs")
        db.execute("DROP TABLE events")
        db.execute("PRAGMA user_version = 2")
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 123)
        assert store.repository_id("Owner/Home") == 123
        assert store.claim_work() is not None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
