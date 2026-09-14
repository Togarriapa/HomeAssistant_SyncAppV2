from ha_syncapp.apply_authorization import ApplyAuthorization


def test_apply_authorization_has_no_externally_callable_privileged_factory():
    assert not hasattr(ApplyAuthorization, "_from_verified_chain")
