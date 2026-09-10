import sys

import ha_syncapp.candidate_semantics as semantic
import pytest


def helper(tmp_path, monkeypatch, body):
    (tmp_path / "_validator_child.py").write_text(body)
    monkeypatch.setattr(semantic, "__file__", str(tmp_path / "candidate_semantics.py"))
    monkeypatch.setattr(semantic, "_CORE_PYTHON", sys.executable)


def test_only_bound_receipt_is_accepted_and_credentials_are_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "credential-canary")
    monkeypatch.setenv("GITHUB_TOKEN", "credential-canary")
    helper(
        tmp_path,
        monkeypatch,
        """
import json, os, sys
assert 'SUPERVISOR_TOKEN' not in os.environ
assert 'GITHUB_TOKEN' not in os.environ
assert sys.flags.isolated == 1
print(json.dumps({'nonce':sys.argv[3], 'version':sys.argv[2], 'result':'passed'}))
""",
    )
    semantic._run_validator(tmp_path, "2026.9.1")


@pytest.mark.parametrize(
    "body",
    [
        "pass",
        "print('credential-canary')",
        "print('{}')",
        "print('x' * 5000)",
        "raise SystemExit(1)",
        "import json; print(json.dumps({'nonce':'wrong','version':'2026.9.1','result':'passed'}))",
        "import json,sys; "
        "print(json.dumps({'nonce':sys.argv[3],'version':'2026.9.2','result':'passed'}))",
        "import json,sys; "
        "print(json.dumps({'nonce':sys.argv[3],'version':sys.argv[2],'result':'invalid'}))",
    ],
)
def test_ambiguous_invalid_or_failed_child_never_grants_evidence(tmp_path, monkeypatch, body):
    helper(tmp_path, monkeypatch, body)
    with pytest.raises(semantic.CandidateSemanticError) as caught:
        semantic._run_validator(tmp_path, "2026.9.1")
    assert "credential-canary" not in str(caught.value)


def test_timeout_reaps_the_child(tmp_path, monkeypatch):
    helper(tmp_path, monkeypatch, "import time; time.sleep(30)")
    monkeypatch.setattr(semantic, "_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(semantic.CandidateSemanticError, match="timed out"):
        semantic._run_validator(tmp_path, "2026.9.1")
