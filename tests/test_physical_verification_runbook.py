from pathlib import Path


RUNBOOK = Path("docs/physical-haos-release-verification.md")


def test_physical_haos_runbook_tracks_both_release_gates_and_authoritative_spec() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "71d284ce447d79b044e332c9bc01ae801dc91947" in text
    assert "#251" in text
    assert "#212" in text
    assert "Raspberry Pi" in text
    assert "read-only" in text
    assert "Retrigger" in text
    assert "AppArmor" in text
    assert "reboot" in text.lower()
    assert "production" in text.lower()
    assert "does not close" in text.lower()
