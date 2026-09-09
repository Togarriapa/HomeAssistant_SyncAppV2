import json
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from ha_syncapp.github_repo import (
    BranchHead,
    RepositoryVerificationError,
    fetch_trusted_branch_head,
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


def test_trusted_branch_head_reverifies_repo_and_returns_exact_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []
    responses = iter(
        [
            FakeResponse({"id": 42, "full_name": "Owner/Home", "private": True}),
            FakeResponse({"name": "main", "commit": {"sha": "a" * 40}}),
        ]
    )

    def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
        assert timeout == 10.0
        requests.append(request)
        return next(responses)

    monkeypatch.setattr("ha_syncapp.github_repo.urlopen", fake_urlopen)

    head = fetch_trusted_branch_head(
        "Owner/Home", "secret-sentinel", expected_id=42, branch="main"
    )

    assert head == BranchHead("Owner/Home", 42, "main", "a" * 40)
    assert [request.full_url for request in requests] == [
        "https://api.github.com/repos/Owner/Home",
        "https://api.github.com/repos/Owner/Home/branches/main",
    ]
    assert all("secret-sentinel" not in request.full_url for request in requests)
    assert all(
        request.get_header("Authorization") == "Bearer secret-sentinel"
        for request in requests
    )


def test_branch_name_is_url_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []
    responses = iter(
        [
            FakeResponse({"id": 42, "full_name": "Owner/Home", "private": True}),
            FakeResponse({"name": "feature/test", "commit": {"sha": "b" * 40}}),
        ]
    )

    def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
        requests.append(request)
        return next(responses)

    monkeypatch.setattr("ha_syncapp.github_repo.urlopen", fake_urlopen)
    head = fetch_trusted_branch_head(
        "Owner/Home", "token", expected_id=42, branch="feature/test"
    )

    assert head.branch == "feature/test"
    assert requests[-1].full_url.endswith("/branches/feature%2Ftest")


@pytest.mark.parametrize(
    "branch",
    [
        "",
        ".",
        "..",
        "bad..name",
        "bad name",
        "bad\\name",
        "/main",
        "main/",
        "a//b",
        "x.lock",
    ],
)
def test_invalid_branch_is_rejected_before_network(branch: str) -> None:
    with pytest.raises(RepositoryVerificationError):
        fetch_trusted_branch_head("Owner/Home", "secret", expected_id=42, branch=branch)


@pytest.mark.parametrize("expected_id", [0, -1, True])
def test_invalid_expected_repository_identity_is_rejected(expected_id: int) -> None:
    with pytest.raises(RepositoryVerificationError):
        fetch_trusted_branch_head("Owner/Home", "secret", expected_id=expected_id)


@pytest.mark.parametrize(
    "branch_payload",
    [
        [],
        {"name": "other", "commit": {"sha": "a" * 40}},
        {"name": "main"},
        {"name": "main", "commit": []},
        {"name": "main", "commit": {"sha": "not-a-sha"}},
        {"name": "main", "commit": {"sha": "A" * 40}},
    ],
)
def test_untrusted_branch_metadata_fails_closed(
    monkeypatch: pytest.MonkeyPatch, branch_payload: object
) -> None:
    responses = iter(
        [
            FakeResponse({"id": 42, "full_name": "Owner/Home", "private": True}),
            FakeResponse(branch_payload),
        ]
    )
    monkeypatch.setattr(
        "ha_syncapp.github_repo.urlopen", lambda request, timeout: next(responses)
    )

    with pytest.raises(RepositoryVerificationError):
        fetch_trusted_branch_head("Owner/Home", "secret", expected_id=42)


def test_repository_identity_mismatch_stops_before_branch_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse({"id": 99, "full_name": "Owner/Home", "private": True})

    monkeypatch.setattr("ha_syncapp.github_repo.urlopen", fake_urlopen)
    with pytest.raises(RepositoryVerificationError):
        fetch_trusted_branch_head("Owner/Home", "secret", expected_id=42)
    assert calls == 1


@pytest.mark.parametrize(
    "error",
    [
        HTTPError("https://api.github.com", 401, "secret-sentinel", {}, None),
        HTTPError("https://api.github.com", 403, "secret-sentinel", {}, None),
        HTTPError("https://api.github.com", 404, "secret-sentinel", {}, None),
        URLError("secret-sentinel"),
        TimeoutError("secret-sentinel"),
    ],
)
def test_branch_transport_failures_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    responses: list[object] = [
        FakeResponse({"id": 42, "full_name": "Owner/Home", "private": True}),
        error,
    ]

    def fake_urlopen(request: Request, *, timeout: float) -> FakeResponse:
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, FakeResponse)
        return response

    monkeypatch.setattr("ha_syncapp.github_repo.urlopen", fake_urlopen)
    with pytest.raises(RepositoryVerificationError) as caught:
        fetch_trusted_branch_head("Owner/Home", "secret-sentinel", expected_id=42)
    assert "secret-sentinel" not in str(caught.value)
