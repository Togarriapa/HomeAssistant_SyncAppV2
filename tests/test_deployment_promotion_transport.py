from __future__ import annotations

import json
from datetime import UTC, datetime

import ha_syncapp.deployment_promotion_transport as transport
from ha_syncapp.deployment_promotion import DeploymentPromotion, PromotionRemoteState
from ha_syncapp.github_repo import BranchHead, RepoIdentity

TOKEN = "github-secret-sentinel"
CANDIDATE = "b" * 40
BASELINE = "a" * 40


def _intent() -> DeploymentPromotion:
    return DeploymentPromotion.create(
        deployment_id="11111111-1111-4111-8111-111111111111",
        target="Owner/Home",
        repository_id=42,
        candidate_sha=CANDIDATE,
        baseline_sha=BASELINE,
        backup_slug="backup-1",
        finalization_sha256="f" * 64,
        known_good_tag="syncapp-known-good-11111111-1111-4111-8111-111111111111",
        phase="planned",
        block_reason="none",
        planned_at=datetime(2026, 9, 20, tzinfo=UTC),
        terminal_at=None,
    )


def test_remote_reader_reproves_private_identity_and_exact_refs(monkeypatch):
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        transport,
        "fetch_and_verify_private_repository",
        lambda target, token, expected_id: RepoIdentity(target, expected_id),
    )

    def branch(target, token, expected_id, branch):
        calls.append((branch, expected_id))
        return BranchHead(
            target,
            expected_id,
            branch,
            CANDIDATE if branch == "candidate" else BASELINE,
        )

    monkeypatch.setattr(transport, "fetch_trusted_branch_head", branch)
    monkeypatch.setattr(transport, "_fetch_tag", lambda *_args: None)

    value = transport.read_promotion_remote_state("Owner/Home", TOKEN, 42, _intent().known_good_tag)

    assert value == PromotionRemoteState(CANDIDATE, BASELINE, None)
    assert calls == [("candidate", 42), ("main", 42)]


def test_publisher_writes_only_missing_refs_without_force(monkeypatch):
    intent = _intent()
    writes: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        transport,
        "fetch_and_verify_private_repository",
        lambda target, token, expected_id: RepoIdentity(target, expected_id),
    )
    monkeypatch.setattr(
        transport,
        "_write_ref",
        lambda target, token, ref, sha, create: writes.append((ref, sha, create)),
    )

    transport.publish_promotion_refs(intent, PromotionRemoteState(CANDIDATE, BASELINE, None), TOKEN)
    assert writes == [
        ("heads/main", CANDIDATE, False),
        (f"tags/{intent.known_good_tag}", CANDIDATE, True),
    ]

    writes.clear()
    transport.publish_promotion_refs(
        intent, PromotionRemoteState(CANDIDATE, CANDIDATE, None), TOKEN
    )
    assert writes == [(f"tags/{intent.known_good_tag}", CANDIDATE, True)]


def test_main_update_payload_is_explicitly_non_force(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({"ref": "refs/heads/main", "object": {"sha": CANDIDATE}}).encode()

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["payload"] = json.loads(request.data)
        captured["authorization"] = request.headers["Authorization"]
        return Response()

    monkeypatch.setattr(transport, "urlopen", open_request)
    transport._write_ref("Owner/Home", TOKEN, "heads/main", CANDIDATE, create=False)

    assert captured["method"] == "PATCH"
    assert captured["payload"] == {"force": False, "sha": CANDIDATE}
    assert captured["url"].endswith("/git/refs/heads/main")
    assert TOKEN not in captured["url"]
