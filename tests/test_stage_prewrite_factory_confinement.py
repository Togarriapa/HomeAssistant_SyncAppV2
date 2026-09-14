from ha_syncapp.stage_prewrite_reproof import StagePrewriteEvidence


def test_stage_prewrite_evidence_has_no_externally_callable_privileged_factory():
    assert not hasattr(StagePrewriteEvidence, "_from_authorization")
