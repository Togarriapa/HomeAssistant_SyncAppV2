from pathlib import Path

import ha_syncapp.candidate_changes as changes_module
import pytest
from ha_syncapp.candidate_changes import (
    CandidateChangeError,
    _TreeEntry,
    detect_candidate_changes,
)
from ha_syncapp.candidate_fetch import CandidateFetch
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry
from ha_syncapp.github_repo import BranchHead, RepositoryVerificationError

TARGET = "Owner/Home"
REPOSITORY_ID = 42
BASELINE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
OID_A = "c" * 40
OID_B = "d" * 40
OID_C = "e" * 40
OID_D = "f" * 40


def _fetched(tmp_path: Path) -> CandidateFetch:
    return CandidateFetch(
        root=tmp_path / "fetch",
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="candidate",
        commit_sha=CANDIDATE_SHA,
        git_ref="refs/syncapp/candidate-fetch",
    )


def _stage(tmp_path: Path, entries: tuple[CandidateStageEntry, ...]) -> CandidateStage:
    root = tmp_path / "stage"
    return CandidateStage(
        root=root,
        tree=root / "tree",
        manifest=root / "manifest.json",
        manifest_sha256="0" * 64,
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="candidate",
        commit_sha=CANDIDATE_SHA,
        entries=entries,
    )


def _stage_entry(path: str, mode: str, object_id: str) -> CandidateStageEntry:
    return CandidateStageEntry(
        path=path,
        git_mode=mode,
        object_id=object_id,
        size=1,
        sha256="1" * 64,
    )


def _head(sha: str = BASELINE_SHA) -> BranchHead:
    return BranchHead(
        target=TARGET,
        repository_id=REPOSITORY_ID,
        branch="main",
        commit_sha=sha,
    )


def _install_detection_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    baseline: tuple[_TreeEntry, ...],
    candidate: tuple[_TreeEntry, ...],
) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr(changes_module.fetch_module, "_validate_token", lambda token: None)
    monkeypatch.setattr(
        changes_module.stage_module,
        "verify_candidate_stage",
        lambda stage: events.append("verify-stage"),
    )
    monkeypatch.setattr(
        changes_module.stage_module,
        "_reprove_fetch",
        lambda fetched: events.append("reprove-fetch"),
    )
    monkeypatch.setattr(changes_module, "_trusted_main", lambda stage, token: _head())
    monkeypatch.setattr(
        changes_module,
        "_fetch_exact_baseline",
        lambda fetched, head, token: events.append("fetch-main"),
    )

    def read_tree(fetched: CandidateFetch, ref: str) -> tuple[_TreeEntry, ...]:
        events.append(f"read:{ref}")
        if ref == fetched.git_ref:
            return candidate
        return baseline

    monkeypatch.setattr(changes_module, "_read_tree", read_tree)
    monkeypatch.setattr(
        changes_module,
        "_delete_baseline_ref",
        lambda fetched: events.append("cleanup-main"),
    )
    return events


def test_detects_add_modify_delete_mode_and_combined_changes_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = (
        _TreeEntry("delete.yaml", "100644", OID_A),
        _TreeEntry("mode.sh", "100644", OID_A),
        _TreeEntry("modify.yaml", "100644", OID_A),
        _TreeEntry("both.sh", "100644", OID_A),
        _TreeEntry("same.yaml", "100644", OID_A),
    )
    candidate = (
        _TreeEntry("add.yaml", "100644", OID_B),
        _TreeEntry("both.sh", "100755", OID_C),
        _TreeEntry("mode.sh", "100755", OID_A),
        _TreeEntry("modify.yaml", "100644", OID_D),
        _TreeEntry("same.yaml", "100644", OID_A),
    )
    stage = _stage(
        tmp_path,
        tuple(_stage_entry(entry.path, entry.git_mode, entry.object_id) for entry in candidate),
    )
    events = _install_detection_fakes(
        monkeypatch,
        baseline=baseline,
        candidate=candidate,
    )

    result = detect_candidate_changes(_fetched(tmp_path), stage, "token")

    assert result.target == TARGET
    assert result.repository_id == REPOSITORY_ID
    assert result.baseline_sha == BASELINE_SHA
    assert result.candidate_sha == CANDIDATE_SHA
    assert [(change.path, change.status) for change in result.changes] == [
        ("add.yaml", "added"),
        ("both.sh", "modified_and_mode_changed"),
        ("delete.yaml", "deleted"),
        ("mode.sh", "mode_changed"),
        ("modify.yaml", "modified"),
    ]
    assert events.count("verify-stage") == 3
    assert events.count("reprove-fetch") == 3
    assert events[-1] == "cleanup-main"


def test_no_change_produces_empty_identity_bound_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = (_TreeEntry("configuration.yaml", "100644", OID_A),)
    stage = _stage(tmp_path, (_stage_entry("configuration.yaml", "100644", OID_A),))
    _install_detection_fakes(monkeypatch, baseline=tree, candidate=tree)

    result = detect_candidate_changes(_fetched(tmp_path), stage, "token")

    assert result.changes == ()
    assert result.baseline_sha == BASELINE_SHA
    assert result.candidate_sha == CANDIDATE_SHA


def test_candidate_git_tree_must_match_stage_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = (_TreeEntry("configuration.yaml", "100755", OID_A),)
    stage = _stage(tmp_path, (_stage_entry("configuration.yaml", "100644", OID_A),))
    events = _install_detection_fakes(monkeypatch, baseline=(), candidate=candidate)

    with pytest.raises(CandidateChangeError, match="does not match staged evidence"):
        detect_candidate_changes(_fetched(tmp_path), stage, "token")

    assert events[-1] == "cleanup-main"


def test_candidate_fetch_and_stage_identity_must_match(tmp_path: Path) -> None:
    fetched = _fetched(tmp_path)
    stage = _stage(tmp_path, ())
    mismatched = CandidateStage(
        root=stage.root,
        tree=stage.tree,
        manifest=stage.manifest,
        manifest_sha256=stage.manifest_sha256,
        target=stage.target,
        repository_id=999,
        branch=stage.branch,
        commit_sha=stage.commit_sha,
        entries=stage.entries,
    )

    with pytest.raises(CandidateChangeError, match="do not match"):
        detect_candidate_changes(fetched, mismatched, "token")


def test_main_movement_during_detection_fails_closed_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = (_TreeEntry("configuration.yaml", "100644", OID_A),)
    stage = _stage(tmp_path, (_stage_entry("configuration.yaml", "100644", OID_A),))
    events = _install_detection_fakes(monkeypatch, baseline=(), candidate=candidate)
    heads = iter((_head(), _head("9" * 40)))
    monkeypatch.setattr(changes_module, "_trusted_main", lambda stage, token: next(heads))

    with pytest.raises(CandidateChangeError, match="main changed"):
        detect_candidate_changes(_fetched(tmp_path), stage, "token")

    assert events[-1] == "cleanup-main"


def test_absent_or_unverifiable_main_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage(tmp_path, ())
    monkeypatch.setattr(changes_module.fetch_module, "_validate_token", lambda token: None)
    monkeypatch.setattr(changes_module.stage_module, "verify_candidate_stage", lambda stage: None)
    monkeypatch.setattr(changes_module.stage_module, "_reprove_fetch", lambda fetched: None)

    def fail(*args: object, **kwargs: object) -> BranchHead:
        raise RepositoryVerificationError("secret upstream detail")

    monkeypatch.setattr(changes_module, "fetch_trusted_branch_head", fail)

    with pytest.raises(CandidateChangeError) as caught:
        detect_candidate_changes(_fetched(tmp_path), stage, "token")

    assert "secret upstream detail" not in str(caught.value)
    assert "trusted Repo B main" in str(caught.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"not-a-tree-record\x00",
        f"120000 blob {OID_A}\tlink\x00".encode(),
        f"160000 commit {OID_A}\tdep\x00".encode(),
        f"100644 blob {OID_A}\t../escape\x00".encode(),
        f"100644 blob {OID_A}\t.storage/.git/config\x00".encode(),
        b"100644 blob bad\tconfig.yaml\x00",
        b"100644 blob " + OID_A.encode() + b"\tbad\xffname\x00",
    ],
)
def test_tree_parser_rejects_unsafe_or_malformed_entries(raw: bytes) -> None:
    with pytest.raises(CandidateChangeError):
        changes_module._parse_tree(raw)


def test_tree_parser_rejects_duplicate_and_conflicting_paths() -> None:
    duplicate = (
        f"100644 blob {OID_A}\ta.yaml\x00100644 blob {OID_B}\ta.yaml\x00"
    ).encode()
    conflict = (
        f"100644 blob {OID_A}\ta\x00100644 blob {OID_B}\ta/file.yaml\x00"
    ).encode()

    with pytest.raises(CandidateChangeError, match="unsafe entry"):
        changes_module._parse_tree(duplicate)
    with pytest.raises(CandidateChangeError, match="conflicting paths"):
        changes_module._parse_tree(conflict)


def test_fetch_baseline_confines_token_to_askpass_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = _fetched(tmp_path)
    fetched.root.mkdir()
    token = "TOP-SECRET-TOKEN"
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    askpass = fetched.root / ".syncapp-askpass"
    monkeypatch.setattr(changes_module.fetch_module, "_git_executable", lambda: "/usr/bin/git")
    monkeypatch.setattr(changes_module.fetch_module, "_create_askpass", lambda root: askpass)
    deleted: list[Path] = []
    monkeypatch.setattr(changes_module.fetch_module, "_delete_askpass", deleted.append)

    def run_git(
        executable: str,
        root: Path,
        arguments: tuple[str, ...],
        **kwargs: object,
    ) -> str:
        del executable, root
        calls.append((arguments, kwargs))
        if arguments[0] == "rev-parse":
            return BASELINE_SHA
        if arguments[0] == "cat-file":
            return "commit"
        return ""

    monkeypatch.setattr(changes_module.fetch_module, "_run_git", run_git)

    changes_module._fetch_exact_baseline(fetched, _head(), token)

    fetch_arguments, fetch_kwargs = calls[0]
    assert fetch_arguments[0] == "fetch"
    assert token not in " ".join(fetch_arguments)
    assert fetch_arguments[-1] == "+refs/heads/main:refs/syncapp/candidate-baseline"
    assert fetch_kwargs == {"token": token, "askpass": askpass}
    assert deleted == [askpass]


def test_fetch_baseline_rejects_observed_sha_mismatch_and_deletes_askpass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = _fetched(tmp_path)
    fetched.root.mkdir()
    askpass = fetched.root / ".syncapp-askpass"
    monkeypatch.setattr(changes_module.fetch_module, "_git_executable", lambda: "/usr/bin/git")
    monkeypatch.setattr(changes_module.fetch_module, "_create_askpass", lambda root: askpass)
    deleted: list[Path] = []
    monkeypatch.setattr(changes_module.fetch_module, "_delete_askpass", deleted.append)

    def run_git(
        executable: str,
        root: Path,
        arguments: tuple[str, ...],
        **kwargs: object,
    ) -> str:
        del executable, root, kwargs
        if arguments[0] == "rev-parse":
            return "8" * 40
        if arguments[0] == "cat-file":
            return "commit"
        return ""

    monkeypatch.setattr(changes_module.fetch_module, "_run_git", run_git)

    with pytest.raises(CandidateChangeError, match="does not match trusted baseline"):
        changes_module._fetch_exact_baseline(fetched, _head(), "token")

    assert deleted == [askpass]
