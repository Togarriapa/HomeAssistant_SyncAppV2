from pathlib import Path
from unittest.mock import Mock

from ha_syncapp.application import Application
from ha_syncapp.config import Config
from ha_syncapp.errors import Failure
from ha_syncapp.journal import Journal
from ha_syncapp.state import StateStore


def test_actions_are_serialized_and_failed_key_tests_do_not_activate(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        app = Application(
            Config(repository="owner/repo", github_metadata_token="sentinel"),
            tmp_path / "syncapp",
            tmp_path / "config",
            Journal(store.connection),
            Mock(),
        )
        app.control.actions.put({"id": "one", "action": "generate"})
        app.step()
        assert app.keys.status()["pending"]
        assert not app.keys.status()["active"]
        repo = Mock()
        repo.test_access.side_effect = Failure("key_test_failed")
        app.repository = Mock(return_value=repo)
        app.control.actions.put({"id": "two", "action": "test"})
        app.step()
        assert app.control.status()["action"]["error"] == "key_test_failed"
        assert app.keys.status()["active"] is None
        repo.test_access.side_effect = None
        app.control.actions.put({"id": "three", "action": "test"})
        app.step()
        app.control.actions.put({"id": "four", "action": "activate"})
        app.step()
        assert app.keys.status()["active"]
        assert app.journal.jobs() == []  # Active key alone does not start sync.


def test_deploy_key_credentials_are_the_only_git_authentication(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        app = Application(
            Config(repository="owner/repo", github_metadata_token="sentinel"),
            tmp_path / "syncapp",
            tmp_path / "config",
            Journal(store.connection),
            Mock(),
        )
        app.keys.generate()
        app.keys.test("owner/repo", Mock())
        app.keys.activate("owner/repo")
        repo = app.repository()
        assert repo.remote == "git@github.com:owner/repo.git"
        assert "sentinel" not in str(repo.env)
        assert "GIT_ASKPASS" not in repo.env
        assert "StrictHostKeyChecking=yes" in repo.env["GIT_SSH_COMMAND"]
