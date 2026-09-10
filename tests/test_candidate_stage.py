import os
from pathlib import Path

import ha_syncapp.candidate_stage as stage_module
import pytest
from ha_syncapp.candidate_fetch import CandidateFetch
from ha_syncapp.candidate_stage import (
    CandidateStageError,
    stage_fetched_candidate,
    verify_candidate_stage,
)

SHA = "a" * 40
OID_A = "b" * 40
OID_B = "c" * 40
TARGET = "Owner/Home"
REPOSITORY_ID = 42


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    fetch_root = tmp_path / "fetch"
    stage_root = tmp_path / "staging"
    home_root = tmp_path / "homeassistant"
    for root in (fetch_root, stage_root, home_root):
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    (fetch_root / ".git").mkdir()
    return fetch_root, stage_root, home_root


def _fetched(fetch_root: Path) -> CandidateFetch:
    return CandidateFetch(
        root=fetch_root,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="candidate",
        commit_sha=SHA,
        git_ref="refs/syncapp/candidate-fetch",
    )


def _install_git_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_tree: bytes,
    blobs: dict[str, bytes],
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def reprove(_fetched: CandidateFetch) -> None:
        calls.append(("reprove",))

    def run_bytes(_root: Path, arguments: tuple[str, ...]) -> bytes:
        calls.append(arguments)
        if arguments[0] == "ls-tree":
            return raw_tree
        if arguments[:2] == ("cat-file", "blob"):
            object_id = arguments[2]
            if object_id not in blobs:
                raise CandidateStageError("candidate staging Git command failed")
            return blobs[object_id]
        raise AssertionError(arguments)

    monkeypatch.setattr(stage_module, "_reprove_fetch", reprove)
    monkeypatch.setattr(stage_module, "_run_git_bytes", run_bytes)
    return calls


def test_stages_exact_candidate_tree_and_reverifies_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    home_sentinel = home_root / "configuration.yaml"
    home_sentinel.write_text("live: untouched\n", encoding="utf-8")
    raw_tree = (
        f"100644 blob {OID_A}\tconfiguration.yaml\0100755 blob {OID_B}\tscripts/tool.sh\0"
    ).encode()
    calls = _install_git_fakes(
        monkeypatch,
        raw_tree=raw_tree,
        blobs={OID_A: b"homeassistant:\n", OID_B: b"#!/bin/sh\ntrue\n"},
    )

    stage = stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)

    assert stage.target == TARGET
    assert stage.repository_id == REPOSITORY_ID
    assert stage.branch == "candidate"
    assert stage.commit_sha == SHA
    assert stage.root.parent == staging_root
    assert stage.root.name.startswith(".git-workspace-candidate-stage-")
    assert stage.root.name.endswith(".tmp")
    assert (stage.tree / "configuration.yaml").read_bytes() == b"homeassistant:\n"
    assert (stage.tree / "scripts/tool.sh").read_bytes() == b"#!/bin/sh\ntrue\n"
    assert (stage.tree / "configuration.yaml").stat().st_mode & 0o777 == 0o600
    assert (stage.tree / "scripts/tool.sh").stat().st_mode & 0o777 == 0o700
    assert home_sentinel.read_text(encoding="utf-8") == "live: untouched\n"
    assert calls.count(("reprove",)) == 2
    assert stage.manifest.read_bytes().endswith(b"\n")

    verify_candidate_stage(stage)


def test_manifest_and_entries_are_deterministic_regardless_of_git_tree_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    raw_tree = (f"100644 blob {OID_B}\tz.yaml\0100644 blob {OID_A}\ta.yaml\0").encode()
    _install_git_fakes(
        monkeypatch,
        raw_tree=raw_tree,
        blobs={OID_A: b"a", OID_B: b"z"},
    )

    stage = stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)
    manifest_text = stage.manifest.read_text(encoding="utf-8")

    assert [entry.path for entry in stage.entries] == ["a.yaml", "z.yaml"]
    assert manifest_text.index("a.yaml") < manifest_text.index("z.yaml")


def test_stage_verification_detects_staged_file_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    _install_git_fakes(
        monkeypatch,
        raw_tree=f"100644 blob {OID_A}\tconfiguration.yaml\0".encode(),
        blobs={OID_A: b"safe\n"},
    )
    stage = stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)
    (stage.tree / "configuration.yaml").write_bytes(b"tampered\n")

    with pytest.raises(CandidateStageError, match="integrity evidence"):
        verify_candidate_stage(stage)


def test_stage_verification_detects_manifest_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    _install_git_fakes(
        monkeypatch,
        raw_tree=f"100644 blob {OID_A}\tconfiguration.yaml\0".encode(),
        blobs={OID_A: b"safe\n"},
    )
    stage = stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)
    stage.manifest.write_bytes(b"{}\n")

    with pytest.raises(CandidateStageError, match="manifest does not match evidence"):
        verify_candidate_stage(stage)


def test_stage_verification_rejects_unexpected_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    _install_git_fakes(
        monkeypatch,
        raw_tree=f"100644 blob {OID_A}\tconfiguration.yaml\0".encode(),
        blobs={OID_A: b"safe\n"},
    )
    stage = stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)
    unexpected = stage.tree / "unexpected"
    unexpected.mkdir(mode=0o700)
    unexpected.chmod(0o700)

    with pytest.raises(CandidateStageError, match="unsafe directory"):
        verify_candidate_stage(stage)


def test_stage_verification_rejects_hardlinked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    _install_git_fakes(
        monkeypatch,
        raw_tree=f"100644 blob {OID_A}\tconfiguration.yaml\0".encode(),
        blobs={OID_A: b"safe\n"},
    )
    stage = stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)
    staged_file = stage.tree / "configuration.yaml"
    outside = tmp_path / "same-bytes"
    outside.write_bytes(b"safe\n")
    outside.chmod(0o600)
    staged_file.unlink()
    os.link(outside, staged_file)

    with pytest.raises(CandidateStageError, match="integrity evidence"):
        verify_candidate_stage(stage)


@pytest.mark.parametrize(
    "raw_tree",
    [
        f"120000 blob {OID_A}\tlink\0".encode(),
        f"160000 commit {OID_A}\tcustom_components/dep\0".encode(),
        f"100644 blob {OID_A}\t../escape\0".encode(),
        f"100644 blob {OID_A}\t/configuration.yaml\0".encode(),
        f"100644 blob {OID_A}\t.storage/.git/config\0".encode(),
        f"100644 blob {OID_A}\ta//b\0".encode(),
        f"100644 blob {OID_A}\ta/./b\0".encode(),
        f"100644 blob {OID_A}\ta/../b\0".encode(),
    ],
)
def test_rejects_unsafe_or_unsupported_tree_entries_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_tree: bytes,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    _install_git_fakes(monkeypatch, raw_tree=raw_tree, blobs={OID_A: b"data"})

    with pytest.raises(CandidateStageError):
        stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)

    assert list(staging_root.iterdir()) == []


def test_rejects_duplicate_tree_paths_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    raw_tree = (f"100644 blob {OID_A}\ta.yaml\0100644 blob {OID_B}\ta.yaml\0").encode()
    _install_git_fakes(monkeypatch, raw_tree=raw_tree, blobs={OID_A: b"a", OID_B: b"b"})

    with pytest.raises(CandidateStageError, match="duplicate path"):
        stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)

    assert list(staging_root.iterdir()) == []


@pytest.mark.parametrize(
    "raw_tree",
    [
        b"not-a-tree-record\0",
        b"100644 blob bad\tconfig.yaml\0",
        b"100644 blob " + OID_A.encode() + b"\tbad\xffname\0",
    ],
)
def test_rejects_malformed_git_tree_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_tree: bytes,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    _install_git_fakes(monkeypatch, raw_tree=raw_tree, blobs={})

    with pytest.raises(CandidateStageError):
        stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)

    assert list(staging_root.iterdir()) == []


def test_blob_failure_cleans_partial_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, staging_root, home_root = _roots(tmp_path)
    raw_tree = (f"100644 blob {OID_A}\ta.yaml\0100644 blob {OID_B}\tb.yaml\0").encode()
    _install_git_fakes(monkeypatch, raw_tree=raw_tree, blobs={OID_A: b"a"})

    with pytest.raises(CandidateStageError, match="Git command failed"):
        stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)

    assert list(staging_root.iterdir()) == []


def test_staging_root_must_be_disjoint_from_home_assistant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, _staging_root, home_root = _roots(tmp_path)
    staging_root = home_root / "staging"
    staging_root.mkdir(mode=0o700)
    staging_root.chmod(0o700)

    with pytest.raises(CandidateStageError, match="overlaps Home Assistant source"):
        stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)

    assert list(staging_root.iterdir()) == []


def test_staging_root_must_not_be_inside_fetch_workspace(
    tmp_path: Path,
) -> None:
    fetch_root, _staging_root, home_root = _roots(tmp_path)
    staging_root = fetch_root / "staging"
    staging_root.mkdir(mode=0o700)
    staging_root.chmod(0o700)

    with pytest.raises(CandidateStageError, match="inside candidate fetch workspace"):
        stage_fetched_candidate(_fetched(fetch_root), staging_root, home_root)

    assert list(staging_root.iterdir()) == []


def test_shared_private_parent_can_hold_fetch_and_stage_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir(mode=0o700)
    workspace_root.chmod(0o700)
    fetch_root = workspace_root / "fetch"
    fetch_root.mkdir(mode=0o700)
    fetch_root.chmod(0o700)
    (fetch_root / ".git").mkdir()
    home_root = tmp_path / "homeassistant"
    home_root.mkdir(mode=0o700)
    home_root.chmod(0o700)
    _install_git_fakes(
        monkeypatch,
        raw_tree=f"100644 blob {OID_A}\tconfiguration.yaml\0".encode(),
        blobs={OID_A: b"safe\n"},
    )

    stage = stage_fetched_candidate(_fetched(fetch_root), workspace_root, home_root)

    assert stage.root.parent == workspace_root
    assert stage.root != fetch_root
    verify_candidate_stage(stage)


def test_reprove_fetch_requires_exact_commit_and_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_root, _staging_root, _home_root = _roots(tmp_path)
    calls: list[tuple[str, ...]] = []
    fetch_module = stage_module.fetch_module

    def verify_workspace(_root: Path) -> None:
        return None

    monkeypatch.setattr(
        fetch_module,
        "_verify_initialized_workspace",
        verify_workspace,
    )
    monkeypatch.setattr(fetch_module, "_git_executable", lambda: "/usr/bin/git")

    def run_git(
        executable: str,
        root: Path,
        arguments: tuple[str, ...],
        **kwargs: object,
    ) -> str:
        del executable, root, kwargs
        calls.append(arguments)
        if arguments[0] == "rev-parse":
            return SHA
        return "commit"

    monkeypatch.setattr(fetch_module, "_run_git", run_git)
    stage_module._reprove_fetch(_fetched(fetch_root))

    assert calls == [
        ("rev-parse", "--verify", "refs/syncapp/candidate-fetch^{commit}"),
        ("cat-file", "-t", "refs/syncapp/candidate-fetch^{commit}"),
    ]
