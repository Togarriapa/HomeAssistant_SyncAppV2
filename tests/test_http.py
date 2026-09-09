import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from ha_syncapp.errors import Failure
from ha_syncapp.http import GitHubGuard, HttpClient
from ha_syncapp.journal import Journal
from ha_syncapp.state import StateStore


def test_ssh_guard_preserves_repository_identity_pinned_by_earlier_v2(tmp_path: Path) -> None:
    with StateStore(tmp_path) as store:
        store.bind_repository("Owner/Home", 123)
        guard = GitHubGuard("owner/home", "sentinel", Journal(store.connection))
        guard.client = Mock()
        guard.client.request.return_value = {
            "id": 124,
            "full_name": "owner/home",
            "private": True,
            "archived": False,
        }
        with pytest.raises(Failure, match="repository_identity_changed"):
            guard.verify()


@pytest.mark.parametrize(
    "repository", ["https://github.com/a/b", "a/b/c", "../b", "a/..", "a/b?x", "a/b\n"]
)
def test_repository_input_cannot_change_transport(repository: str) -> None:
    with pytest.raises(Failure):
        GitHubGuard(repository, "sentinel", Mock())


def test_private_repository_identity_is_bound_and_rechecked() -> None:
    journal = Mock()
    journal.db.execute.return_value.fetchall.return_value = []
    journal.value.return_value = None
    guard = GitHubGuard("owner/repo", "sentinel", journal)
    guard.client = Mock()
    guard.client.request.return_value = {
        "id": 17,
        "full_name": "owner/repo",
        "private": True,
        "visibility": "private",
        "archived": False,
    }
    guard.verify()
    journal.set.assert_called_once_with("repository_id", 17)
    journal.value.return_value = 17
    guard.client.request.return_value["private"] = False
    with pytest.raises(Failure, match="repository_not_private"):
        guard.verify()
    guard.client.request.return_value["private"] = True
    guard.client.request.return_value["id"] = 18
    with pytest.raises(Failure, match="repository_identity_changed"):
        guard.verify()


def test_http_sanitizes_exceptions_and_limits_response() -> None:
    client = HttpClient("https://api.github.com", "sentinel")
    client.opener = Mock()
    client.opener.open.side_effect = OSError("sentinel")
    with pytest.raises(Failure) as error:
        client.request("GET", "/repos/a/b")
    assert "sentinel" not in str(error.value)
    assert error.value.retryable
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"ok": True}).encode()
    client.opener.open.side_effect = None
    client.opener.open.return_value = response
    assert client.request("GET", "/repos/a/b") == {"ok": True}
    response.read.return_value = b"x" * 33
    with pytest.raises(Failure, match="api_response_too_large"):
        client.request("GET", "/repos/a/b", limit=32)
