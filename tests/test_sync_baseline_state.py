import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ha_syncapp.state import StateError, StateStore, SynchronizationBaseline

TARGET = "Owner/Home"
BRANCH = "main"
SNAPSHOT_A = "a" * 64
SNAPSHOT_B = "b" * 64
COMMIT_A = "1" * 40
COMMIT_B = "2" * 40
WHEN_A = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)
WHEN_B = WHEN_A + timedelta(minutes=5)


def test_synchronization_baseline_is_absent_until_recorded_and_survives_reopen(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path) as store:
        assert store.synchronization_baseline(TARGET, BRANCH) is None
        recorded = store.record_synchronization_baseline(
            TARGET, BRANCH, SNAPSHOT_A, COMMIT_A, synchronized_at=WHEN_A
        )
        assert recorded == SynchronizationBaseline(
            target=TARGET,
            branch=BRANCH,
            snapshot_id=SNAPSHOT_A,
            commit_sha=COMMIT_A,
            synchronized_at=WHEN_A,
        )

    with StateStore(tmp_path) as store:
        assert store.synchronization_baseline(TARGET, BRANCH) == recorded


def test_synchronization_baseline_is_atomically_replaced(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.record_synchronization_baseline(
            TARGET, BRANCH, SNAPSHOT_A, COMMIT_A, synchronized_at=WHEN_A
        )
        replacement = store.record_synchronization_baseline(
            TARGET, BRANCH, SNAPSHOT_B, COMMIT_B, synchronized_at=WHEN_B
        )
        assert store.synchronization_baseline(TARGET, BRANCH) == replacement
        assert replacement.snapshot_id == SNAPSHOT_B
        assert replacement.commit_sha == COMMIT_B
        assert replacement.synchronized_at == WHEN_B


@pytest.mark.parametrize(
    ("target", "branch", "snapshot_id", "commit_sha", "when"),
    [
        ("", BRANCH, SNAPSHOT_A, COMMIT_A, WHEN_A),
        ("Owner/Home\nsecret-sentinel", BRANCH, SNAPSHOT_A, COMMIT_A, WHEN_A),
        (TARGET, "", SNAPSHOT_A, COMMIT_A, WHEN_A),
        (TARGET, "bad branch", SNAPSHOT_A, COMMIT_A, WHEN_A),
        (TARGET, BRANCH, "x" * 64, COMMIT_A, WHEN_A),
        (TARGET, BRANCH, SNAPSHOT_A, "not-a-sha", WHEN_A),
        (TARGET, BRANCH, SNAPSHOT_A, COMMIT_A, datetime(2026, 9, 9, 18, 0)),
    ],
)
def test_invalid_synchronization_baseline_is_rejected_without_disclosure(
    tmp_path: Path,
    target: str,
    branch: str,
    snapshot_id: str,
    commit_sha: str,
    when: datetime,
) -> None:
    with StateStore(tmp_path) as store, pytest.raises(StateError) as error:
        store.record_synchronization_baseline(
            target, branch, snapshot_id, commit_sha, synchronized_at=when
        )
    assert "secret-sentinel" not in str(error.value)


def test_corrupt_persisted_synchronization_baseline_fails_closed(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.record_synchronization_baseline(
            TARGET, BRANCH, SNAPSHOT_A, COMMIT_A, synchronized_at=WHEN_A
        )

    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE synchronization_baseline SET snapshot_id = ? WHERE target = ? AND branch = ?",
            ("corrupt", TARGET, BRANCH),
        )

    with StateStore(tmp_path) as store, pytest.raises(StateError):
        store.synchronization_baseline(TARGET, BRANCH)


def test_schema_v3_migrates_baselines_without_losing_existing_state(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        first = store.start_run()
        store.bind_repository(TARGET, 12345)
        store.enqueue_work("runtime", "existing", now=WHEN_A)
        store.finish_run()

    path = tmp_path / "syncapp/state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE synchronization_baseline")
        db.execute("DROP TABLE prepared_deployment")
        db.execute("PRAGMA user_version = 3")

    with StateStore(tmp_path) as store:
        second = store.start_run()
        assert second.installation_id == first.installation_id
        assert store.repository_id(TARGET) == 12345
        assert store.claim_work(now=WHEN_A) is not None
        assert store.synchronization_baseline(TARGET, BRANCH) is None
        store.record_synchronization_baseline(
            TARGET, BRANCH, SNAPSHOT_A, COMMIT_A, synchronized_at=WHEN_A
        )
        assert store.synchronization_baseline(TARGET, BRANCH) is not None
