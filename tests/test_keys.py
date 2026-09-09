import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from ha_syncapp.errors import Failure
from ha_syncapp.journal import Journal
from ha_syncapp.keys import KeyManager
from ha_syncapp.state import StateStore


def test_key_generation_testing_and_safe_rotation(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        manager = KeyManager(tmp_path / "syncapp/keys", Journal(store.connection))
        first = manager.generate()
        assert first["public_key"].startswith("ssh-ed25519 ")
        assert "PRIVATE KEY" not in str(first)
        assert manager.generate() == first
        with pytest.raises(Failure):
            manager.activate("owner/repo")
        tester = Mock()
        manager.test("owner/repo", tester)
        manager.activate("owner/repo")
        active = manager.identity()
        assert stat.S_IMODE(active.private_key.stat().st_mode) == 0o600
        assert "StrictHostKeyChecking=yes" in active.command()
        replacement = manager.generate()
        assert replacement["fingerprint"] != first["fingerprint"]
        tester.side_effect = Failure("key_test_failed")
        with pytest.raises(Failure):
            manager.test("owner/repo", tester)
        assert manager.identity().private_key == active.private_key
        with pytest.raises(Failure):
            manager.activate("owner/repo")
        tester.side_effect = None
        manager.test("owner/repo", tester)
        with pytest.raises(Failure):
            manager.activate("owner/other")
        manager.activate("owner/repo")
        assert manager.identity().private_key != active.private_key


def test_no_key_means_no_transport(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        manager = KeyManager(tmp_path / "syncapp/keys", Journal(store.connection))
        with pytest.raises(Failure, match="deploy_key_missing"):
            manager.identity()


def test_pinned_github_host_key_matches_official_fingerprint(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        manager = KeyManager(tmp_path / "syncapp/keys", Journal(store.connection))
        output = subprocess.check_output(
            ["ssh-keygen", "-l", "-E", "sha256", "-f", str(manager.hosts)]
        )
        assert b"SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" in output


def test_successive_rotations_bound_retained_key_files(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        directory = tmp_path / "syncapp/keys"
        manager = KeyManager(directory, Journal(store.connection))
        for _ in range(5):
            manager.generate()
            manager.test("owner/repo", Mock())
            manager.activate("owner/repo")
        assert len(list(directory.iterdir())) == 5  # Active, previous, their publics, pinned hosts.
