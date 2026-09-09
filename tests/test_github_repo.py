import json
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from ha_syncapp.github_repo import (
    RepoIdentity,
    RepositoryVerificationError,
    fetch_and_verify_private_repository,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._body = BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def test_authenticated_private_repository_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse({"id": 12345, "full_name": "Owner/Home", "private": True})

    monkeypatch.setattr("ha_syncapp.github_repo.urlopen", fake_urlopen)
    identity = fetch_and_verify_private_repository("Owner/Home", "secret-sentinel")
    assert identity == RepoIdentity(target="Owner/Home", repository_id=12345)
    request = captured["request"]
    assert isinstance(request, Request)
    assert request.full_url == "https://api.github.com/repos/Owner/Home"
    assert "secret-sentinel" not in request.full_url
    assert request.get_header("Authorization") == "Bearer secret-sentinel"
    assert captured["timeout"] == 10.0


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 1, "full_name": "Owner/Home", "private": False},
        {"id": 1, "full_name": "Other/Home", "private": True},
        {"id": 0, "full_name": "Owner/Home", "private": True},
        {"id": True, "full_name": "Owner/Home", "private": True},
        {"id": 1, "full_name": "Owner/Home"},
        [],
    ],
)
def test_untrusted_repository_metadata_fails_closed(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    monkeypatch.setattr(
        "ha_syncapp.github_repo.urlopen", lambda request, timeout: FakeResponse(payload)
    )
    with pytest.raises(RepositoryVerificationError):
        fetch_and_verify_private_repository("Owner/Home", "secret-sentinel")


def test_repository_replacement_id_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_syncapp.github_repo.urlopen",
        lambda request, timeout: FakeResponse(
            {"id": 222, "full_name": "Owner/Home", "private": True}
        ),
    )
    with pytest.raises(RepositoryVerificationError):
        fetch_and_verify_private_repository("Owner/Home", "secret-sentinel", expected_id=111)


@pytest.mark.parametrize(
    "error",
    [
        HTTPError("https://api.github.com/repos/Owner/Home", 401, "bad", {}, None),
        HTTPError("https://api.github.com/repos/Owner/Home", 403, "rate", {}, None),
        HTTPError("https://api.github.com/repos/Owner/Home", 404, "missing", {}, None),
        HTTPError("https://api.github.com/repos/Owner/Home", 500, "server", {}, None),
        URLError("network secret-sentinel"),
        TimeoutError("secret-sentinel"),
    ],
)
def test_transport_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def fail(request: Request, *, timeout: float) -> FakeResponse:
        raise error

    monkeypatch.setattr("ha_syncapp.github_repo.urlopen", fail)
    with pytest.raises(RepositoryVerificationError) as caught:
        fetch_and_verify_private_repository("Owner/Home", "secret-sentinel")
    assert "secret-sentinel" not in str(caught.value)
    assert "network" not in str(caught.value)


def test_oversized_or_malformed_metadata_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    class RawResponse(FakeResponse):
        def __init__(self, body: bytes) -> None:
            self._body = BytesIO(body)

    for body in (b"{" + b"x" * 65536, b"not-json"):
        def respond(request: Request, timeout: float, payload: bytes = body) -> RawResponse:
            return RawResponse(payload)

        monkeypatch.setattr("ha_syncapp.github_repo.urlopen", respond)
        with pytest.raises(RepositoryVerificationError):
            fetch_and_verify_private_repository("Owner/Home", "secret-sentinel")
