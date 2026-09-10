from __future__ import annotations

import os
import subprocess
from pathlib import Path

import ha_syncapp.candidate_stage as stage_module
import pytest
from ha_syncapp.candidate_fetch import CandidateFetch
from ha_syncapp.candidate_stage import (
    CandidateStageError,
    CandidateTreeFile,
    stage_fetched_candidate,
    verify_candidate_stage,
)

TARGET = "Owner/Home"
REPOSITORY_ID = 42
FETCH_REF = "refs/syncapp/candidate-fetch"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fetched_candidate(tmp_path: Path) -> tuple[CandidateFetch, Path]:
    root = tmp_path / "fetch"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "SyncApp Test")
    _git(root, "config", "user.email", "syncapp@example.invalid")

    (root / "configuration.yaml").write_text("homeassistant:\n  name: Test\n")
    nested = root / "packages"
    nested.mkdir()
    script = nested / "prepare.sh"
    script.write_text("#!/bin/sh\necho safe\n")
    script.chmod(0o755)
    _git(root, "add", "--", "configuration.yaml", "packages/prepare.sh")
    _git(root, "commit", "--quiet", "-m", "candidate")
    sha = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", FETCH_REF, sha)

    (root / "configuration.yaml").unlink()
    script.unlink()
    nested.rmdir()
    fetched = CandidateFetch(
        root=root,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="candidate",
        commit_sha=sha,
        git_ref=FETCH_REF,
    )
    home = tmp_path / "homeassistant"
    home.mkdir()
    return fetched, home


def _staging_root(tmp_path: Path) -> Path:
    root = tmp_path / "candidate-staging"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def test_stages_exact_commit_without_checkout_and_reverifies_snapshot(tmp_path: Path) -> None:
    fetched, home = _fetched_candidate(tmp_path)
    stage = stage_fetched_candidate(fetched, _staging_root(tmp_path), home)

    assert stage.target == TARGET
    assert stage.repository_id == REPOSITORY_ID
    assert stage.branch == "candidate"
    assert stage.commit_sha == fetched.commit_sha
    assert len(stage.stage_id) == 64
    assert len(stage.snapshot_id) == 64
    assert stage.tree_path.joinpath("configuration.yaml").read_text() == (
        "homeassistant:\n  name: Test\n"
    )
    script = stage.tree_path / "packages" / "prepare.sh"
    assert script.read_text() == "#!/bin/sh\necho safe\n"
    assert script.stat().st_mode & 0o777 == 0o755
    assert (stage.tree_path / "configuration.yaml").stat().st_mode & 0o777 == 0o644
    assert verify_candidate_stage(stage.root) == stage
    assert not any(
        path.name.startswith(".candidate-source-")
        for path in _staging_root(tmp_path).iterdir()
    )


def test_stage_detects_staged_byte_tamper(tmp_path: Path) -> None:
    fetched, home = _fetched_candidate(tmp_path)
    stage = stage_fetched_candidate(fetched, _staging_root(tmp_path), home)
    (stage.tree_path / "configuration.yaml").write_text("tampered\n")

    with pytest.raises(CandidateStageError, match="snapshot verification failed"):
        verify_candidate_stage(stage.root)


def test_stage_detects_candidate_manifest_tamper(tmp_path: Path) -> None:
    fetched, home = _fetched_candidate(tmp_path)
    stage = stage_fetched_candidate(fetched, _staging_root(tmp_path), home)
    document = stage.manifest_path.read_text().replace(TARGET, "Other/Home")
    stage.manifest_path.write_text(document)

    with pytest.raises(CandidateStageError, match="digest is invalid"):
        verify_candidate_stage(stage.root)


def test_fetch_ref_change_is_rejected_before_materialization(tmp_path: Path) -> None:
    fetched, home = _fetched_candidate(tmp_path)
    root = fetched.root
    (root / "other.yaml").write_text("different: true\n")
    _git(root, "add", "--", "other.yaml")
    _git(root, "commit", "--quiet", "-m", "moved")
    moved = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", FETCH_REF, moved)
    (root / "other.yaml").unlink()
    staging = _staging_root(tmp_path)

    with pytest.raises(CandidateStageError, match="no longer matches evidence"):
        stage_fetched_candidate(fetched, staging, home)

    assert list(staging.iterdir()) == []


def test_staging_root_must_be_disjoint_from_live_home_assistant(tmp_path: Path) -> None:
    fetched, home = _fetched_candidate(tmp_path)
    staging = home / "candidate-staging"
    staging.mkdir(mode=0o700)
    staging.chmod(0o700)

    with pytest.raises(CandidateStageError, match="overlaps protected boundary"):
        stage_fetched_candidate(fetched, staging, home)

    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(
    "record",
    [
        b"120000 blob " + b"a" * 40 + b" 4\tlink",
        b"160000 commit " + b"a" * 40 + b" -\tsubmodule",
        b"100644 blob " + b"a" * 40 + b" 1\t../escape",
        b"100644 blob " + b"a" * 40 + b" 1\tconfig/.git/index",
        b"100644 blob " + b"a" * 40 + b" 1\t/absolute",
        b"100644 blob " + b"a" * 40 + b" 1\tbad\xffname",
        b"malformed",
    ],
)
def test_rejects_unsafe_or_malformed_git_tree_records(record: bytes) -> None:
    with pytest.raises(CandidateStageError):
        stage_module._parse_tree_record(record)


def test_tree_listing_rejects_duplicate_and_prefix_collisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fetched, _ = _fetched_candidate(tmp_path)
    oid = b"a" * 40
    duplicate = (
        b"100644 blob " + oid + b" 1\ta\0" + b"100644 blob " + oid + b" 1\ta\0"
    )
    prefix = (
        b"100644 blob "
        + oid
        + b" 1\ta\0"
        + b"100644 blob "
        + oid
        + b" 1\ta/b\0"
    )

    for raw in (duplicate, prefix):
        monkeypatch.setattr(stage_module, "_run_git_bytes", lambda *args, **kwargs: raw)
        with pytest.raises(CandidateStageError, match="colliding paths"):
            stage_module._list_candidate_tree(fetched)


def test_blob_size_mismatch_is_rejected_and_partial_file_removed(tmp_path: Path) -> None:
    fetched, _ = _fetched_candidate(tmp_path)
    oid = _git(fetched.root, "rev-parse", f"{fetched.commit_sha}:configuration.yaml")
    destination = tmp_path / "materialized.yaml"
    item = CandidateTreeFile(path="configuration.yaml", blob_oid=oid, size=1, mode=0o644)

    with pytest.raises(CandidateStageError, match="materialization is invalid"):
        stage_module._materialize_blob(fetched.root, item, destination)

    assert destination.exists() is False


def test_materialization_failure_cleans_source_and_incomplete_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched, home = _fetched_candidate(tmp_path)
    staging = _staging_root(tmp_path)

    def fail(*args: object, **kwargs: object) -> None:
        raise CandidateStageError("candidate blob materialization failed")

    monkeypatch.setattr(stage_module, "_materialize_blob", fail)
    with pytest.raises(CandidateStageError, match="materialization failed"):
        stage_fetched_candidate(fetched, staging, home)

    assert not any(path.name.startswith(".candidate-source-") for path in staging.iterdir())
    snapshots = staging / ".candidate-snapshots"
    assert snapshots.is_dir()
    assert list(snapshots.iterdir()) == []


def test_insufficient_disk_space_fails_before_blob_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched, home = _fetched_candidate(tmp_path)
    staging = _staging_root(tmp_path)
    monkeypatch.setattr(
        stage_module.shutil,
        "disk_usage",
        lambda path: os.statvfs_result((0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    )

    with pytest.raises(CandidateStageError, match="insufficient free space"):
        stage_fetched_candidate(fetched, staging, home)


def test_unexpected_stage_root_entry_fails_closed(tmp_path: Path) -> None:
    fetched, home = _fetched_candidate(tmp_path)
    stage = stage_fetched_candidate(fetched, _staging_root(tmp_path), home)
    (stage.root / "unexpected").write_text("nope")

    with pytest.raises(CandidateStageError, match="unexpected root entries"):
        verify_candidate_stage(stage.root)
