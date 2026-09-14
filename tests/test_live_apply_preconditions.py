import hashlib
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from ha_syncapp.live_apply_plan import LiveApplyOperation, LiveApplyPlan
from ha_syncapp.live_apply_preconditions import (
    LiveApplyPreconditionError,
    prove_live_apply_preconditions,
)


def _git_blob_id(data: bytes, algorithm: str = "sha1") -> str:
    digest = hashlib.sha1(usedforsecurity=False) if algorithm == "sha1" else hashlib.sha256()
    digest.update(f"blob {len(data)}\0".encode())
    digest.update(data)
    return digest.hexdigest()


def _plan(*operations: LiveApplyOperation) -> LiveApplyPlan:
    plan = object.__new__(LiveApplyPlan)
    values = {
        "deployment_id": "deploy-1",
        "target": "owner/private-repo",
        "repository_id": 123,
        "baseline_sha": "1" * 40,
        "candidate_sha": "2" * 40,
        "stage_manifest_sha256": "3" * 64,
        "operations": operations,
    }
    for name, value in values.items():
        object.__setattr__(plan, name, value)
    return plan


def _modified(path: str, baseline: bytes, *, mode: str = "100644") -> LiveApplyOperation:
    return LiveApplyOperation(
        path=path,
        status="modified",
        baseline_mode=mode,
        baseline_object_id=_git_blob_id(baseline),
        candidate_mode=mode,
        candidate_object_id="4" * 40,
        staged_size=7,
        staged_sha256="5" * 64,
    )


def test_proves_absent_added_and_matching_existing_paths(tmp_path: Path) -> None:
    existing = tmp_path / "automations.yaml"
    baseline = b"old: true\n"
    existing.write_bytes(baseline)
    os.chmod(existing, 0o644)
    plan = _plan(
        _modified("automations.yaml", baseline),
        LiveApplyOperation(
            path="new.yaml",
            status="added",
            baseline_mode=None,
            baseline_object_id=None,
            candidate_mode="100644",
            candidate_object_id="6" * 40,
            staged_size=3,
            staged_sha256="7" * 64,
        ),
    )

    evidence = prove_live_apply_preconditions(plan, tmp_path)

    assert evidence.deployment_id == plan.deployment_id
    assert evidence.target == plan.target
    assert evidence.repository_id == plan.repository_id
    assert evidence.baseline_sha == plan.baseline_sha
    assert evidence.candidate_sha == plan.candidate_sha
    assert evidence.stage_manifest_sha256 == plan.stage_manifest_sha256
    assert evidence.root == str(tmp_path)
    assert evidence.verified_paths == ("automations.yaml", "new.yaml")
    with pytest.raises(FrozenInstanceError):
        evidence.baseline_sha = "0" * 40  # type: ignore[misc]


def test_supports_sha256_git_blob_ids(tmp_path: Path) -> None:
    data = b"sha256 repository\n"
    path = tmp_path / "configuration.yaml"
    path.write_bytes(data)
    os.chmod(path, 0o644)
    operation = LiveApplyOperation(
        path="configuration.yaml",
        status="modified",
        baseline_mode="100644",
        baseline_object_id=_git_blob_id(data, "sha256"),
        candidate_mode="100644",
        candidate_object_id="8" * 64,
        staged_size=1,
        staged_sha256="9" * 64,
    )

    evidence = prove_live_apply_preconditions(_plan(operation), tmp_path)

    assert evidence.verified_paths == ("configuration.yaml",)


@pytest.mark.parametrize(
    ("status", "exists"),
    [
        ("added", True),
        ("deleted", False),
        ("modified", False),
        ("mode_changed", False),
        ("modified_and_mode_changed", False),
    ],
)
def test_rejects_live_presence_mismatches(tmp_path: Path, status: str, exists: bool) -> None:
    baseline = b"baseline\n"
    if exists:
        (tmp_path / "item.yaml").write_bytes(baseline)
    operation = LiveApplyOperation(
        path="item.yaml",
        status=status,
        baseline_mode=None if status == "added" else "100644",
        baseline_object_id=None if status == "added" else _git_blob_id(baseline),
        candidate_mode=None if status == "deleted" else "100644",
        candidate_object_id=None if status == "deleted" else "a" * 40,
        staged_size=None if status == "deleted" else 1,
        staged_sha256=None if status == "deleted" else "b" * 64,
    )

    with pytest.raises(LiveApplyPreconditionError, match="live path precondition mismatch"):
        prove_live_apply_preconditions(_plan(operation), tmp_path)


def test_rejects_content_or_mode_drift(tmp_path: Path) -> None:
    data = b"current\n"
    path = tmp_path / "scripts.yaml"
    path.write_bytes(data)
    os.chmod(path, 0o755)
    plan = _plan(_modified("scripts.yaml", b"different\n", mode="100644"))

    with pytest.raises(LiveApplyPreconditionError, match="live path precondition mismatch"):
        prove_live_apply_preconditions(plan, tmp_path)


def test_rejects_symlink_and_does_not_follow_it(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_bytes(b"secret\n")
    (tmp_path / "linked.yaml").symlink_to(outside)
    plan = _plan(_modified("linked.yaml", b"secret\n"))

    with pytest.raises(LiveApplyPreconditionError, match="live path type is unsafe"):
        prove_live_apply_preconditions(plan, tmp_path)


def test_rejects_symlinked_parent_component(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-parent"
    outside.mkdir()
    (outside / "configuration.yaml").write_bytes(b"secret\n")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    plan = _plan(_modified("linked/configuration.yaml", b"secret\n"))

    with pytest.raises(LiveApplyPreconditionError, match="live path type is unsafe"):
        prove_live_apply_preconditions(plan, tmp_path)


def test_rejects_non_regular_file(tmp_path: Path) -> None:
    (tmp_path / "folder.yaml").mkdir()
    plan = _plan(_modified("folder.yaml", b"anything\n"))

    with pytest.raises(LiveApplyPreconditionError, match="live path type is unsafe"):
        prove_live_apply_preconditions(plan, tmp_path)


def test_rejects_relative_root_duplicate_unsafe_and_unsorted_paths(tmp_path: Path) -> None:
    operation = LiveApplyOperation(
        path="../escape.yaml",
        status="added",
        baseline_mode=None,
        baseline_object_id=None,
        candidate_mode="100644",
        candidate_object_id="c" * 40,
        staged_size=1,
        staged_sha256="d" * 64,
    )
    with pytest.raises(LiveApplyPreconditionError, match="Home Assistant root is invalid"):
        prove_live_apply_preconditions(_plan(operation), Path("relative"))
    with pytest.raises(LiveApplyPreconditionError, match="Apply plan path is unsafe"):
        prove_live_apply_preconditions(_plan(operation), tmp_path)

    duplicate = _modified("same.yaml", b"x")
    with pytest.raises(LiveApplyPreconditionError, match="duplicate affected paths"):
        prove_live_apply_preconditions(_plan(duplicate, duplicate), tmp_path)

    with pytest.raises(LiveApplyPreconditionError, match="operation order is invalid"):
        prove_live_apply_preconditions(
            _plan(_modified("z.yaml", b"z"), _modified("a.yaml", b"a")), tmp_path
        )


def test_rejects_wrong_plan_type_and_sanitizes_filesystem_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(LiveApplyPreconditionError, match="Apply plan evidence is invalid"):
        prove_live_apply_preconditions(object(), tmp_path)  # type: ignore[arg-type]

    data = b"baseline\n"
    path = tmp_path / "configuration.yaml"
    path.write_bytes(data)
    plan = _plan(_modified("configuration.yaml", data))

    def explode(fd: int, size: int) -> bytes:
        raise OSError("PRIVATE-NESTED-DETAIL")

    monkeypatch.setattr(os, "read", explode)
    with pytest.raises(LiveApplyPreconditionError) as error:
        prove_live_apply_preconditions(plan, tmp_path)
    assert "PRIVATE-NESTED-DETAIL" not in str(error.value)
    assert str(error.value) == "live path inspection failed"


def test_detects_plan_drift_during_filesystem_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"baseline\n"
    path = tmp_path / "configuration.yaml"
    path.write_bytes(data)
    plan = _plan(_modified("configuration.yaml", data))
    original = os.read

    def mutate_after_read(fd: int, size: int) -> bytes:
        result = original(fd, size)
        object.__setattr__(plan, "candidate_sha", "e" * 40)
        return result

    monkeypatch.setattr(os, "read", mutate_after_read)
    with pytest.raises(LiveApplyPreconditionError, match="Apply plan changed during inspection"):
        prove_live_apply_preconditions(plan, tmp_path)
