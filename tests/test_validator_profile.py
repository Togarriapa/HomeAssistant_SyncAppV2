import pytest
from ha_syncapp._validator_child import _verify_apparmor_label


@pytest.mark.parametrize(
    "label",
    [
        b"homeassistant_syncapp_v2//validator (enforce)",
        b"local_homeassistant_syncapp_v2//validator (enforce)\n",
        b"abc12345_homeassistant_syncapp_v2//validator (enforce)",
    ],
)
def test_only_enforced_supervisor_adjusted_validator_profile_is_accepted(label: bytes) -> None:
    _verify_apparmor_label(label)


@pytest.mark.parametrize(
    "label",
    [
        b"unconfined",
        b"docker-default (enforce)",
        b"local_homeassistant_syncapp_v2 (enforce)",
        b"local_homeassistant_syncapp_v2//validator (complain)",
        b"other_app//validator (enforce)",
        b"local_homeassistant_syncapp_v2//validator (enforce) extra",
        b"a" * 257,
    ],
)
def test_missing_wrong_or_complain_profile_fails_closed(label: bytes) -> None:
    with pytest.raises(RuntimeError, match="AppArmor validator profile"):
        _verify_apparmor_label(label)
