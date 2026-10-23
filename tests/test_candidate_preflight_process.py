from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from ha_syncapp import candidate_preflight_process as preflight
from ha_syncapp import candidate_preflight_service as preflight_service_module
from ha_syncapp import candidate_stage as stage_module
from ha_syncapp.candidate_changes import CandidateChange, CandidateChanges
from ha_syncapp.candidate_dependency_execution import execute_candidate_dependency_once
from ha_syncapp.candidate_deploy_key_ingress import DeployKeyCandidateIngress
from ha_syncapp.candidate_detection import CandidateObservation
from ha_syncapp.candidate_detection_service import CandidateDetectionService
from ha_syncapp.candidate_fetch import CandidateFetch
from ha_syncapp.candidate_fetch_stage_execution import (
    CandidateFetchStageExecutionError,
    execute_candidate_fetch_stage_once,
)
from ha_syncapp.candidate_integrity import CandidateIntegrity
from ha_syncapp.candidate_fetch_stage_retrigger import run_candidate_fetch_stage_retrigger_pass
from ha_syncapp.candidate_integrity_execution import execute_candidate_integrity_once
from ha_syncapp.candidate_orchestration import (
    CandidateOrchestration,
    dependencies_analyzed_candidate_orchestration,
    integrity_verified_candidate_orchestration,
    load_candidate_orchestration,
    risk_classified_candidate_orchestration,
    semantically_validated_candidate_orchestration,
    staged_candidate_orchestration,
    static_validated_candidate_orchestration,
)
from ha_syncapp.candidate_preflight_service import CandidatePreflightService
from ha_syncapp.candidate_risk_execution import execute_candidate_risk_once
from ha_syncapp.candidate_semantic_execution import execute_candidate_semantic_once
from ha_syncapp.candidate_semantics import CandidateSemanticError, CandidateSemanticValidation
from ha_syncapp.candidate_stage import CandidateStage, CandidateStageEntry
from ha_syncapp.candidate_static_execution import execute_candidate_static_once
from ha_syncapp.deploy_key_access import DeployKeyAccessProof
from ha_syncapp.runtime_inventory import RuntimeInventoryInput
from ha_syncapp.state import StateError, StateStore

TARGET = "Owner/Home"
TOKEN = "github-secret-sentinel"
NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def _store(tmp_path: Path) -> StateStore:
    data = tmp_path / "data"
    data.mkdir()
    store = StateStore(data)
    store.__enter__()
    store.bind_repository(TARGET, 123)
    return store


def _insert_backup_ready_orchestration(store: StateStore, candidate_sha: str) -> None:
    orchestration = CandidateOrchestration.create(
        candidate_sha=candidate_sha,
        target=TARGET,
        repository_id=123,
        registered_at=NOW,
    )
    orchestration = staged_candidate_orchestration(orchestration, updated_at=NOW)
    orchestration = integrity_verified_candidate_orchestration(orchestration, updated_at=NOW)
    orchestration = dependencies_analyzed_candidate_orchestration(orchestration, updated_at=NOW)
    orchestration = risk_classified_candidate_orchestration(orchestration, updated_at=NOW)
    orchestration = static_validated_candidate_orchestration(orchestration, updated_at=NOW)
    orchestration = semantically_validated_candidate_orchestration(orchestration, updated_at=NOW)
    with store._connection as database:
        database.execute(
            "INSERT INTO candidate_orchestration VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            orchestration.database_values(),
        )


def _make_fetch(
    parent: Path,
    candidate_sha: str,
) -> CandidateFetch:
    root = parent / f"candidate-fetch-{uuid4().hex}"
    root.mkdir(mode=0o700)
    return CandidateFetch(
        root,
        TARGET,
        123,
        "candidate",
        candidate_sha,
        "refs/syncapp/candidate-fetch",
    )


def _make_stage(staging_root: Path, candidate_sha: str) -> CandidateStage:
    content = b"homeassistant:\n  name: Candidate preflight test\n"
    temporary_root = staging_root / f".git-workspace-candidate-stage-{uuid4().hex}.tmp"
    tree = temporary_root / "tree"
    tree.mkdir(parents=True, mode=0o700)
    temporary_root.chmod(0o700)
    tree.chmod(0o700)
    path = tree / "configuration.yaml"
    path.write_bytes(content)
    path.chmod(0o600)
    object_id = hashlib.sha1(
        f"blob {len(content)}\0".encode() + content,
        usedforsecurity=False,
    ).hexdigest()
    entry = CandidateStageEntry(
        "configuration.yaml",
        "100644",
        object_id,
        len(content),
        hashlib.sha256(content).hexdigest(),
    )
    manifest_bytes = stage_module._manifest_bytes(
        target=TARGET,
        repository_id=123,
        branch="candidate",
        commit_sha=candidate_sha,
        entries=(entry,),
    )
    manifest = temporary_root / "manifest.json"
    manifest.write_bytes(manifest_bytes)
    manifest.chmod(0o600)
    return CandidateStage(
        temporary_root,
        tree,
        manifest,
        hashlib.sha256(manifest_bytes).hexdigest(),
        TARGET,
        123,
        "candidate",
        candidate_sha,
        (entry,),
    )


def _run(
    store: StateStore,
    tmp_path: Path,
    *,
    when: datetime = NOW,
    fetch_stage_executor: preflight.Executor | None = None,
):
    home = tmp_path / "homeassistant"
    home.mkdir(exist_ok=True)
    for name in ("fetch", "analysis", "stage"):
        (tmp_path / name).mkdir(exist_ok=True)
    options: dict[str, object] = {
        "core_token": "supervisor-token",
        "reference_time": when,
    }
    if fetch_stage_executor is not None:
        options["fetch_stage_executor"] = fetch_stage_executor
    return preflight.run_candidate_preflight_process(
        store,
        home,
        tmp_path / "fetch",
        tmp_path / "analysis",
        tmp_path / "stage",
        TARGET,
        TOKEN,
        **options,
    )


def test_process_claims_oldest_due_pending_candidate_for_one_action(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_sha = "a" * 40
    second_sha = "b" * 40
    store.enqueue_work("candidate", first_sha, now=NOW - timedelta(seconds=2))
    store.enqueue_work("candidate", second_sha, now=NOW - timedelta(seconds=1))
    observed: list[tuple[CandidateOrchestration, dict[str, object]]] = []

    def fetch_stage(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        assert current_store is store
        observed.append((orchestration, options))
        return SimpleNamespace(checkpoint=SimpleNamespace(phase="completed"))

    try:
        result = _run(store, tmp_path, fetch_stage_executor=fetch_stage)
        first = store._get_work("candidate", first_sha)
        second = store._get_work("candidate", second_sha)
    finally:
        store.__exit__(None, None, None)

    assert result.action == "fetch_stage"
    assert result.outcome == "advanced"
    assert len(observed) == 1
    assert observed[0][0].candidate_sha == first_sha
    assert observed[0][0].next_action == "fetch_stage"
    assert observed[0][1]["token"] == TOKEN
    assert observed[0][1]["deploy_key_ingress"] is None
    assert first.status == second.status == "pending"
    assert first.updated_at == NOW
    assert second.updated_at == NOW - timedelta(seconds=1)


def test_process_leaves_non_pending_exhausted_and_backup_ready_work_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    candidates = {
        "running": "a" * 40,
        "retry_due": "b" * 40,
        "retry_future": "d" * 40,
        "blocked": "e" * 40,
        "terminal": "f" * 40,
        "exhausted": "g" * 40,
    }
    for name, candidate_sha in candidates.items():
        store.enqueue_work("candidate", candidate_sha, now=NOW - timedelta(days=1))
        claimed = store.claim_work_kind("candidate", now=NOW)
        assert claimed is not None and claimed.work_key == candidate_sha
        if name == "running":
            continue
        if name in {"retry_due", "retry_future"}:
            failed = store.fail_work(claimed, transient=True, now=NOW)
            if name == "retry_due":
                with store._connection as database:
                    database.execute(
                        "UPDATE work SET next_attempt_at=? WHERE work_kind='candidate' "
                        "AND work_key=?",
                        ((NOW - timedelta(seconds=1)).isoformat(), candidate_sha),
                    )
            assert failed.status == "retry"
        elif name == "blocked":
            store.fail_work(claimed, transient=False, now=NOW)
        elif name == "terminal":
            store.complete_work(claimed, now=NOW)
        else:
            with store._connection as database:
                database.execute(
                    "UPDATE work SET status='pending', attempts=?, next_attempt_at=? "
                    "WHERE work_kind='candidate' AND work_key=?",
                    (StateStore.MAX_WORK_ATTEMPTS, NOW.isoformat(), candidate_sha),
                )

    backup_sha = "c" * 40
    store.enqueue_work("candidate", backup_sha, now=NOW - timedelta(days=1))
    _insert_backup_ready_orchestration(store, backup_sha)
    with store._connection as database:
        before = database.execute(
            "SELECT work_kind,work_key,status,attempts,created_at,updated_at,next_attempt_at "
            "FROM work ORDER BY work_key"
        ).fetchall()
        backup_before = database.execute(
            "SELECT candidate_sha,phase,next_action,record_sha256 FROM candidate_orchestration "
            "WHERE candidate_sha=?",
            (backup_sha,),
        ).fetchone()

    try:
        result = _run(
            store,
            tmp_path,
            fetch_stage_executor=lambda *args, **kwargs: pytest.fail(
                "excluded candidate invoked Fetch/Stage"
            ),
        )
        with store._connection as database:
            after = database.execute(
                "SELECT work_kind,work_key,status,attempts,created_at,updated_at,next_attempt_at "
                "FROM work ORDER BY work_key"
            ).fetchall()
            backup_after = database.execute(
                "SELECT candidate_sha,phase,next_action,record_sha256 FROM candidate_orchestration "
                "WHERE candidate_sha=?",
                (backup_sha,),
            ).fetchone()
    finally:
        store.__exit__(None, None, None)

    assert result.action is None
    assert result.outcome == "idle"
    assert before == after
    assert backup_before == backup_after


def test_transient_executor_failure_preserves_backoff_for_retrigger(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    candidate_sha = "8" * 40
    store.enqueue_work("candidate", candidate_sha, now=NOW)
    calls = 0

    def unavailable(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise CandidateFetchStageExecutionError("network token sentinel", transient=True)

    try:
        first = _run(store, tmp_path, fetch_stage_executor=unavailable)
        failed = store._get_work("candidate", candidate_sha)
        second = _run(store, tmp_path, fetch_stage_executor=unavailable)
        still_retry = store._get_work("candidate", candidate_sha)
    finally:
        store.__exit__(None, None, None)

    assert first.outcome == "retry"
    assert failed.status == "retry" and failed.attempts == 1
    assert failed.next_attempt_at == NOW + timedelta(seconds=60)
    assert second.action is None and second.outcome == "idle"
    assert still_retry == failed
    assert calls == 1


def test_deterministic_executor_failure_blocks_the_exact_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    candidate_sha = "9" * 40
    store.enqueue_work("candidate", candidate_sha, now=NOW)

    def invalid(*args: object, **kwargs: object) -> object:
        raise CandidateFetchStageExecutionError("private candidate detail", transient=False)

    try:
        result = _run(store, tmp_path, fetch_stage_executor=invalid)
        blocked = store._get_work("candidate", candidate_sha)
        orchestration = store._connection.execute(
            "SELECT phase,next_action FROM candidate_orchestration WHERE candidate_sha=?",
            (candidate_sha,),
        ).fetchone()
    finally:
        store.__exit__(None, None, None)

    assert result.outcome == "blocked"
    assert blocked.status == "blocked"
    assert orchestration == ("detected", "fetch_stage")
    assert candidate_sha not in repr(result)


def test_stale_target_or_phase_action_authority_is_not_claimed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    stale_sha = "1" * 40
    mismatched_sha = "2" * 40
    store.bind_repository("Owner/Other", 999)
    store.enqueue_work("candidate", stale_sha, now=NOW)
    store.enqueue_work("candidate", mismatched_sha, now=NOW)
    stale = CandidateOrchestration.create(
        candidate_sha=stale_sha,
        target="Owner/Other",
        repository_id=999,
        registered_at=NOW,
    )
    valid = CandidateOrchestration.create(
        candidate_sha=mismatched_sha,
        target=TARGET,
        repository_id=123,
        registered_at=NOW,
    )
    with store._connection as database:
        database.execute(
            "INSERT INTO candidate_orchestration VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            stale.database_values(),
        )
        database.execute(
            "INSERT INTO candidate_orchestration VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            valid.database_values(),
        )
        database.execute(
            "UPDATE candidate_orchestration SET next_action='analyze' WHERE candidate_sha=?",
            (mismatched_sha,),
        )
        before = database.execute(
            "SELECT work_key,status,attempts,updated_at,next_attempt_at FROM work "
            "WHERE work_kind='candidate' ORDER BY work_key"
        ).fetchall()

    try:
        result = _run(
            store,
            tmp_path,
            fetch_stage_executor=lambda *args, **kwargs: pytest.fail(
                "stale candidate authority invoked Fetch/Stage"
            ),
        )
        with store._connection as database:
            after = database.execute(
                "SELECT work_key,status,attempts,updated_at,next_attempt_at FROM work "
                "WHERE work_kind='candidate' ORDER BY work_key"
            ).fetchall()
    finally:
        store.__exit__(None, None, None)

    assert result.action is None and result.outcome == "idle"
    assert before == after


def test_invalid_inputs_fail_before_claiming_candidate_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate_sha = "3" * 40
    store.enqueue_work("candidate", candidate_sha, now=NOW)
    home = tmp_path / "homeassistant"
    home.mkdir()
    for name in ("fetch", "analysis", "stage"):
        (tmp_path / name).mkdir()

    try:
        with pytest.raises(preflight.CandidatePreflightProcessError, match="inputs are invalid"):
            preflight.run_candidate_preflight_process(
                store,
                home,
                tmp_path / "fetch",
                tmp_path / "analysis",
                tmp_path / "stage",
                TARGET,
                "",
                reference_time=NOW,
            )
        untouched = store._get_work("candidate", candidate_sha)
    finally:
        store.__exit__(None, None, None)

    assert untouched.status == "pending" and untouched.attempts == 0


def test_repository_state_failure_is_sanitized_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    candidate_sha = "4" * 40
    store.enqueue_work("candidate", candidate_sha, now=NOW)
    secret = "private repository identity sentinel"

    def fail_lookup(self: StateStore, target: str) -> int | None:
        del self, target
        raise StateError(secret)

    monkeypatch.setattr(StateStore, "repository_id", fail_lookup)

    try:
        with pytest.raises(
            preflight.CandidatePreflightProcessError,
            match="repository is unavailable",
        ) as caught:
            _run(store, tmp_path)
        untouched = store._get_work("candidate", candidate_sha)
    finally:
        store.__exit__(None, None, None)

    assert secret not in str(caught.value)
    assert untouched.status == "pending" and untouched.attempts == 0


def test_process_runs_the_six_real_read_only_executors_to_backup_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    candidate_sha = "5" * 40
    baseline_sha = "6" * 40
    home = tmp_path / "homeassistant"
    fetch_root = tmp_path / "fetch"
    analysis_root = tmp_path / "analysis"
    staging_root = tmp_path / "stage"
    for root in (home, fetch_root, analysis_root, staging_root):
        root.mkdir(mode=0o700)
    live_configuration = home / "configuration.yaml"
    live_bytes = b"homeassistant:\n  name: Original live configuration\n"
    live_configuration.write_bytes(live_bytes)
    live_configuration.chmod(0o600)

    proof = DeployKeyAccessProof(
        TARGET,
        123,
        "SHA256:" + "A" * 43,
        str(uuid4()),
        0,
        "d" * 64,
    )
    ingress = DeployKeyCandidateIngress(
        proof,
        tmp_path / "keys",
        tmp_path / "ingress-work",
        known_hosts_file=tmp_path / "known-hosts",
        git_executable=tmp_path / "git",
        ssh_executable=tmp_path / "ssh",
    )
    calls: list[str] = []
    observed = CandidateObservation(TARGET, 123, "candidate", candidate_sha)

    def ingress_observe(
        current_ingress: DeployKeyCandidateIngress,
        target: str,
        *,
        expected_id: int,
    ) -> CandidateObservation:
        assert current_ingress is ingress
        assert (target, expected_id) == (TARGET, 123)
        calls.append("deploy_key_observe")
        return observed

    def ingress_fetch(
        current_ingress: DeployKeyCandidateIngress,
        observation: CandidateObservation,
        expected_sha: str,
        workspace_root: Path,
        home_assistant_root: Path,
    ) -> CandidateFetch:
        assert current_ingress is ingress
        assert (observation, expected_sha) == (observed, candidate_sha)
        calls.append("deploy_key_fetch")
        return _make_fetch(workspace_root, candidate_sha)

    monkeypatch.setattr(DeployKeyCandidateIngress, "observe", ingress_observe)
    monkeypatch.setattr(DeployKeyCandidateIngress, "fetch", ingress_fetch)
    monkeypatch.setattr(stage_module, "_reprove_fetch", lambda _fetched: None)

    detection_service = CandidateDetectionService(
        store,
        TARGET,
        None,
        deploy_key_ingress=ingress,
        interval_seconds=1.0,
    )

    def run_fetch_stage(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        assert options["token"] is None
        assert options["deploy_key_ingress"] is ingress
        calls.append("fetch_stage")
        return execute_candidate_fetch_stage_once(
            current_store,
            orchestration,
            token=options["token"],
            deploy_key_ingress=options["deploy_key_ingress"],
            workspace_root=options["workspace_root"],
            staging_root=options["staging_root"],
            home_assistant_root=options["home_assistant_root"],
            stager=lambda _fetched, root, _home: _make_stage(root, candidate_sha),
            now=options["now"],
        )

    def run_integrity(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        assert options["token"] == TOKEN
        calls.append("analyze")

        def changes_for_candidate(
            _fetched: CandidateFetch,
            stage: CandidateStage,
            _token: str,
        ) -> CandidateChanges:
            entry = stage.entries[0]
            return CandidateChanges(
                TARGET,
                123,
                baseline_sha,
                candidate_sha,
                (
                    CandidateChange(
                        entry.path,
                        "added",
                        None,
                        None,
                        entry.git_mode,
                        entry.object_id,
                    ),
                ),
            )

        return execute_candidate_integrity_once(
            current_store,
            orchestration,
            token=options["token"],
            workspace_root=options["workspace_root"],
            staging_root=options["staging_root"],
            home_assistant_root=options["home_assistant_root"],
            observer=lambda target, token, *, expected_id: observed,
            fetcher=lambda observation, expected_sha, token, workspace, home: _make_fetch(
                workspace, expected_sha
            ),
            change_detector=changes_for_candidate,
            now=options["now"],
        )

    runtime = RuntimeInventoryInput(
        manifest={"core_config": {"version": "2026.10.2"}},
        homeassistant={"entities": [], "services": []},
    )

    def run_dependencies(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        assert options["core_token"] == "supervisor-token"
        calls.append("analyze_dependencies")
        return execute_candidate_dependency_once(
            current_store,
            orchestration,
            core_token=options["core_token"],
            staging_root=options["staging_root"],
            home_assistant_root=options["home_assistant_root"],
            runtime_collector=lambda *, token: runtime,
            now=options["now"],
        )

    def run_risk(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        calls.append("classify_risk")
        return execute_candidate_risk_once(current_store, orchestration, now=options["now"])

    def run_static(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        calls.append("validate")
        return execute_candidate_static_once(
            current_store,
            orchestration,
            staging_root=options["staging_root"],
            home_assistant_root=options["home_assistant_root"],
            now=options["now"],
        )

    def run_semantic(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        calls.append("validate_semantics")

        def validator(
            static: object,
            integrity: CandidateIntegrity,
            stage: CandidateStage,
            _dependencies: object,
            _impact: object,
            risk: object,
            _runtime: RuntimeInventoryInput,
            version: object,
        ) -> CandidateSemanticValidation:
            return CandidateSemanticValidation(
                orchestration.target,
                orchestration.repository_id,
                integrity.baseline_sha,
                stage.commit_sha,
                stage.manifest_sha256,
                static.runtime_sha256,
                risk.level,
                version.version,
            )

        return execute_candidate_semantic_once(
            current_store,
            orchestration,
            staging_root=options["staging_root"],
            home_assistant_root=options["home_assistant_root"],
            validator=validator,
            now=options["now"],
        )

    executors = {
        "fetch_stage_executor": run_fetch_stage,
        "integrity_executor": run_integrity,
        "dependency_executor": run_dependencies,
        "risk_executor": run_risk,
        "static_executor": run_static,
        "semantic_executor": run_semantic,
    }

    def process_with_synthetic_transport(*args: object, **options: object) -> object:
        return preflight.run_candidate_preflight_process(
            *args,
            **options,
            **executors,
        )

    monkeypatch.setattr(
        preflight_service_module,
        "run_candidate_preflight_process",
        process_with_synthetic_transport,
    )
    routine_service = CandidatePreflightService(
        store,
        home,
        fetch_root,
        analysis_root,
        staging_root,
        TARGET,
        TOKEN,
        deploy_key_ingress=ingress,
        core_token="supervisor-token",
    )
    outcomes = []
    try:
        detection_service.start(0.0)
        routine_service.start(0.0)
        assert routine_service.tick(0.999).due is False
        detection = detection_service.tick(1.0).detection
        assert detection is not None and detection.work is not None
        assert detection.work.work_key == candidate_sha
        for offset, expected_action in enumerate(
            (
                "fetch_stage",
                "analyze",
                "analyze_dependencies",
                "classify_risk",
                "validate",
                "validate_semantics",
            )
        ):
            tick = routine_service.tick(1.0 + offset)
            assert tick.due and tick.process is not None
            assert tick.process.action == expected_action
            assert tick.process.outcome == "advanced"
            outcomes.append(tick.process)
        final_orchestration = load_candidate_orchestration(store, candidate_sha)
        work = store._get_work("candidate", candidate_sha)
        idle = routine_service.tick(7.0)
    finally:
        routine_service.stop()
        detection_service.stop()
        store.__exit__(None, None, None)

    assert [item.outcome for item in outcomes] == ["advanced"] * 6
    assert calls == [
        "deploy_key_observe",
        "fetch_stage",
        "deploy_key_observe",
        "deploy_key_fetch",
        "analyze",
        "analyze_dependencies",
        "classify_risk",
        "validate",
        "validate_semantics",
    ]
    assert final_orchestration is not None
    assert (final_orchestration.phase, final_orchestration.next_action) == (
        "semantically_validated",
        "prepare_backup",
    )
    assert work.status == "pending" and work.attempts == 0
    assert idle.due and idle.process is not None
    assert idle.process.action is None and idle.process.outcome == "idle"
    assert live_configuration.read_bytes() == live_bytes


def test_token_fetch_stage_uses_only_configured_token_transport(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate_sha = "7" * 40
    store.enqueue_work("candidate", candidate_sha, now=NOW)
    home = tmp_path / "homeassistant"
    fetch_root = tmp_path / "fetch"
    analysis_root = tmp_path / "analysis"
    staging_root = tmp_path / "stage"
    for root in (home, fetch_root, analysis_root, staging_root):
        root.mkdir(mode=0o700)
    calls: list[str] = []

    def fetch_stage(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        assert options["token"] == TOKEN
        assert options["deploy_key_ingress"] is None

        def observe(target: str, token: str, *, expected_id: int) -> CandidateObservation:
            assert (target, token, expected_id) == (TARGET, TOKEN, 123)
            calls.append("observe")
            return CandidateObservation(TARGET, 123, "candidate", candidate_sha)

        def fetcher(
            observation: CandidateObservation,
            expected_sha: str,
            token: str,
            workspace_root: Path,
            _home: Path,
        ) -> CandidateFetch:
            assert observation.commit_sha == expected_sha == candidate_sha
            assert token == TOKEN
            calls.append("fetch")
            return _make_fetch(workspace_root, candidate_sha)

        def stage(
            _fetched: CandidateFetch,
            root: Path,
            _home: Path,
        ) -> CandidateStage:
            calls.append("stage")
            return _make_stage(root, candidate_sha)

        return execute_candidate_fetch_stage_once(
            current_store,
            orchestration,
            token=options["token"],
            workspace_root=options["workspace_root"],
            staging_root=options["staging_root"],
            home_assistant_root=options["home_assistant_root"],
            observer=observe,
            fetcher=fetcher,
            stager=stage,
            now=options["now"],
        )

    try:
        result = preflight.run_candidate_preflight_process(
            store,
            home,
            fetch_root,
            analysis_root,
            staging_root,
            TARGET,
            TOKEN,
            reference_time=NOW,
            fetch_stage_executor=fetch_stage,
        )
        orchestration = load_candidate_orchestration(store, candidate_sha)
        work = store._get_work("candidate", candidate_sha)
    finally:
        store.__exit__(None, None, None)

    assert result.action == "fetch_stage" and result.outcome == "advanced"
    assert calls == ["observe", "fetch", "stage"]
    assert orchestration is not None and orchestration.phase == "staged"
    assert work.status == "pending" and work.attempts == 0


def test_changed_deploy_key_observation_blocks_without_token_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    candidate_sha = "8" * 40
    store.enqueue_work("candidate", candidate_sha, now=NOW)
    home = tmp_path / "homeassistant"
    fetch_root = tmp_path / "fetch"
    analysis_root = tmp_path / "analysis"
    staging_root = tmp_path / "stage"
    for root in (home, fetch_root, analysis_root, staging_root):
        root.mkdir(mode=0o700)
    ingress = DeployKeyCandidateIngress(
        DeployKeyAccessProof(TARGET, 123, "SHA256:" + "B" * 43, str(uuid4()), 0, "e" * 64),
        tmp_path / "keys",
        tmp_path / "ingress-work",
    )
    calls: list[str] = []

    def changed_observation(
        _ingress: DeployKeyCandidateIngress,
        _target: str,
        *,
        expected_id: int,
    ) -> CandidateObservation:
        assert expected_id == 123
        calls.append("observe")
        return CandidateObservation(TARGET, 124, "candidate", candidate_sha)

    def forbidden_fetch(*_args: object, **_kwargs: object) -> CandidateFetch:
        pytest.fail("changed SSH proof must not fall back to token Git")

    monkeypatch.setattr(DeployKeyCandidateIngress, "observe", changed_observation)

    def fetch_stage(
        current_store: StateStore,
        orchestration: CandidateOrchestration,
        **options: object,
    ) -> object:
        assert options["token"] is None
        assert options["deploy_key_ingress"] is ingress
        calls.append("fetch_stage")
        return execute_candidate_fetch_stage_once(
            current_store,
            orchestration,
            token=options["token"],
            deploy_key_ingress=options["deploy_key_ingress"],
            workspace_root=options["workspace_root"],
            staging_root=options["staging_root"],
            home_assistant_root=options["home_assistant_root"],
            fetcher=forbidden_fetch,
            now=options["now"],
        )

    try:
        result = preflight.run_candidate_preflight_process(
            store,
            home,
            fetch_root,
            analysis_root,
            staging_root,
            TARGET,
            TOKEN,
            deploy_key_ingress=ingress,
            reference_time=NOW,
            fetch_stage_executor=fetch_stage,
        )
        blocked = store._get_work("candidate", candidate_sha)
        orchestration = load_candidate_orchestration(store, candidate_sha)
        next_tick = preflight.run_candidate_preflight_process(
            store,
            home,
            fetch_root,
            analysis_root,
            staging_root,
            TARGET,
            TOKEN,
            deploy_key_ingress=ingress,
            reference_time=NOW + timedelta(seconds=1),
            fetch_stage_executor=fetch_stage,
        )
        after = store._get_work("candidate", candidate_sha)
    finally:
        store.__exit__(None, None, None)

    assert result.action == "fetch_stage" and result.outcome == "blocked"
    assert blocked.status == "blocked" and blocked.attempts == 1
    assert orchestration is not None and orchestration.phase == "detected"
    assert next_tick.action is None and next_tick.outcome == "idle"
    assert after == blocked
    assert calls == ["fetch_stage", "observe"]


def test_static_rejection_is_blocked_atomically_without_defer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_candidate_static_execution import _static_ready

    store, orchestration = _static_ready(tmp_path, monkeypatch, b"broken: [yaml\n")
    candidate_sha = orchestration.candidate_sha
    when = orchestration.updated_at + timedelta(seconds=1)
    store.defer_work(store._get_work("candidate", candidate_sha), now=when)
    home = tmp_path / "homeassistant"
    fetch_root = tmp_path / "process-fetch"
    analysis_root = tmp_path / "process-analysis"
    staging_root = tmp_path / "process-stage"
    for root in (fetch_root, analysis_root, staging_root):
        root.mkdir(mode=0o700)

    try:
        result = preflight.run_candidate_preflight_process(
            store,
            home,
            fetch_root,
            analysis_root,
            staging_root,
            orchestration.target,
            TOKEN,
            reference_time=when,
        )
        blocked = store._get_work("candidate", candidate_sha)
        current = load_candidate_orchestration(store, candidate_sha)
        next_tick = preflight.run_candidate_preflight_process(
            store,
            home,
            fetch_root,
            analysis_root,
            staging_root,
            orchestration.target,
            TOKEN,
            reference_time=when + timedelta(seconds=1),
        )
        after = store._get_work("candidate", candidate_sha)
    finally:
        store.__exit__(None, None, None)

    assert result.action == "validate" and result.outcome == "blocked"
    assert blocked.status == "blocked" and blocked.attempts == 1
    assert current is not None and (current.phase, current.next_action) == ("blocked", "none")
    assert next_tick.action is None and next_tick.outcome == "idle"
    assert after == blocked


def test_semantic_rejection_is_blocked_atomically_without_defer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_candidate_semantic_execution import _semantic_ready

    store, orchestration, _expected = _semantic_ready(tmp_path, monkeypatch)
    candidate_sha = orchestration.candidate_sha
    when = orchestration.updated_at + timedelta(seconds=1)
    store.defer_work(store._get_work("candidate", candidate_sha), now=when)
    home = tmp_path / "homeassistant"
    fetch_root = tmp_path / "process-fetch"
    analysis_root = tmp_path / "process-analysis"
    staging_root = tmp_path / "process-stage"
    for root in (fetch_root, analysis_root, staging_root):
        root.mkdir(mode=0o700)

    def reject_semantics(
        current_store: StateStore,
        current: CandidateOrchestration,
        **options: object,
    ) -> object:
        def reject(*_args: object) -> CandidateSemanticValidation:
            raise CandidateSemanticError("private semantic sentinel")

        return execute_candidate_semantic_once(
            current_store,
            current,
            staging_root=options["staging_root"],
            home_assistant_root=options["home_assistant_root"],
            validator=reject,
            now=options["now"],
        )

    try:
        result = preflight.run_candidate_preflight_process(
            store,
            home,
            fetch_root,
            analysis_root,
            staging_root,
            orchestration.target,
            TOKEN,
            reference_time=when,
            semantic_executor=reject_semantics,
        )
        blocked = store._get_work("candidate", candidate_sha)
        current = load_candidate_orchestration(store, candidate_sha)
        next_tick = preflight.run_candidate_preflight_process(
            store,
            home,
            fetch_root,
            analysis_root,
            staging_root,
            orchestration.target,
            TOKEN,
            reference_time=when + timedelta(seconds=1),
            semantic_executor=reject_semantics,
        )
        after = store._get_work("candidate", candidate_sha)
    finally:
        store.__exit__(None, None, None)

    assert result.action == "validate_semantics" and result.outcome == "blocked"
    assert blocked.status == "blocked" and blocked.attempts == 1
    assert current is not None and (current.phase, current.next_action) == ("blocked", "none")
    assert next_tick.action is None and next_tick.outcome == "idle"
    assert after == blocked


def test_normal_process_leaves_running_and_due_retry_for_retrigger(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    retry_sha = "a" * 40
    running_sha = "b" * 40
    store.enqueue_work("candidate", retry_sha, now=NOW - timedelta(seconds=2))
    retry_claim = store.claim_work_kind("candidate", now=NOW)
    assert retry_claim is not None
    retry = store.fail_work(retry_claim, transient=True, now=NOW)
    store.enqueue_work("candidate", running_sha, now=NOW - timedelta(seconds=1))
    running = store.claim_work_kind("candidate", now=NOW)
    assert running is not None and running.status == "running"
    with store._connection as database:
        database.execute(
            "UPDATE work SET next_attempt_at=? WHERE work_kind='candidate' AND work_key=?",
            ((NOW - timedelta(seconds=1)).isoformat(), retry_sha),
        )
    with store._connection as database:
        before = database.execute(
            "SELECT work_key,status,attempts,updated_at,next_attempt_at FROM work "
            "WHERE work_kind='candidate' ORDER BY work_key"
        ).fetchall()

    home = tmp_path / "homeassistant"
    home.mkdir(mode=0o700)
    fetch_root = tmp_path / "fetch"
    analysis_root = tmp_path / "analysis"
    staging_root = tmp_path / "stage"
    for root in (fetch_root, analysis_root, staging_root):
        root.mkdir(mode=0o700)
    try:
        result = preflight.run_candidate_preflight_process(
            store,
            home,
            fetch_root,
            analysis_root,
            staging_root,
            TARGET,
            TOKEN,
            reference_time=NOW,
        )
        with store._connection as database:
            after_routine = database.execute(
                "SELECT work_key,status,attempts,updated_at,next_attempt_at FROM work "
                "WHERE work_kind='candidate' ORDER BY work_key"
            ).fetchall()

        def completed(
            current_store: StateStore,
            orchestration: CandidateOrchestration,
            **options: object,
        ) -> object:
            candidate = orchestration.candidate_sha
            observation = CandidateObservation(TARGET, 123, "candidate", candidate)
            return execute_candidate_fetch_stage_once(
                current_store,
                orchestration,
                token=options["token"],
                workspace_root=options["workspace_root"],
                staging_root=options["staging_root"],
                home_assistant_root=options["home_assistant_root"],
                observer=lambda target, token, *, expected_id: observation,
                fetcher=lambda _observation, expected_sha, _token, workspace, _home: _make_fetch(
                    workspace, expected_sha
                ),
                stager=lambda _fetched, root, _home: _make_stage(root, candidate),
                now=options["now"],
            )

        first = run_candidate_fetch_stage_retrigger_pass(
            store,
            home,
            fetch_root,
            staging_root,
            TARGET,
            TOKEN,
            reference_time=NOW + timedelta(seconds=1),
            executor=completed,
        )
        after_first_retrigger = (
            store._get_work("candidate", retry_sha),
            store._get_work("candidate", running_sha),
        )
        second = run_candidate_fetch_stage_retrigger_pass(
            store,
            home,
            fetch_root,
            staging_root,
            TARGET,
            TOKEN,
            reference_time=NOW + timedelta(seconds=2),
            executor=completed,
        )
        after_second_retrigger = (
            store._get_work("candidate", retry_sha),
            store._get_work("candidate", running_sha),
        )
    finally:
        store.__exit__(None, None, None)

    assert result.action is None and result.outcome == "idle"
    assert before == after_routine
    assert retry.status == "retry"
    assert first.recovered_interrupted == 1 and first.processed == "completed"
    assert second.recovered_interrupted == 0 and second.processed == "completed"
    assert all(item.status == "pending" and item.attempts == 0 for item in after_second_retrigger)
    assert {item.work_key for item in after_first_retrigger if item.status == "pending"} == {
        retry_sha
    }
