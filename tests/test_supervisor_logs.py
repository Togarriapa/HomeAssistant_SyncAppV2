from datetime import UTC, datetime

import pytest
from ha_syncapp.supervisor_logs import (
    SupervisorLogError,
    SupervisorLogResponse,
    collect_supervisor_logs,
)

REFERENCE = datetime(2026, 9, 10, 4, 45, tzinfo=UTC)
TOKEN = "supervisor-secret"


def test_collects_core_and_supervisor_as_distinct_deterministic_records() -> None:
    calls: list[tuple[str, str, dict[str, str], float, int]] = []

    def transport(method, url, headers, timeout, limit):
        calls.append((method, url, dict(headers), timeout, limit))
        body = b"core one\ncore two\n" if "/core/" in url else b"supervisor one\n"
        return SupervisorLogResponse(200, "text/plain; charset=utf-8", body)

    first = collect_supervisor_logs(
        token=TOKEN,
        reference_time=REFERENCE,
        transport=transport,
    )
    second = collect_supervisor_logs(
        token=TOKEN,
        reference_time=REFERENCE,
        transport=transport,
    )

    assert first == second
    assert [record.category for record in first] == [
        "home-assistant",
        "home-assistant",
        "supervisor",
    ]
    assert [record.message for record in first] == ["core one", "core two", "supervisor one"]
    assert all(record.timestamp == REFERENCE for record in first)
    assert len({record.record_id for record in first}) == 3
    assert len(calls) == 4
    for method, url, headers, timeout, limit in calls:
        assert method == "GET"
        assert url.startswith("http://supervisor/")
        assert headers == {"Accept": "text/plain", "Authorization": f"Bearer {TOKEN}"}
        assert TOKEN not in url
        assert timeout == 10.0
        assert limit == 4 * 1024 * 1024


def test_collection_is_all_or_nothing_when_required_source_fails() -> None:
    calls = 0

    def transport(method, url, headers, timeout, limit):
        nonlocal calls
        del method, headers, timeout, limit
        calls += 1
        if "/core/" in url:
            return SupervisorLogResponse(200, "text/plain", b"core\n")
        return SupervisorLogResponse(503, "text/plain", b"unavailable")

    with pytest.raises(SupervisorLogError, match="supervisor log request failed"):
        collect_supervisor_logs(token=TOKEN, reference_time=REFERENCE, transport=transport)
    assert calls == 2


def test_transport_failure_does_not_expose_token() -> None:
    def transport(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(TOKEN)

    with pytest.raises(SupervisorLogError) as captured:
        collect_supervisor_logs(token=TOKEN, reference_time=REFERENCE, transport=transport)
    assert TOKEN not in str(captured.value)
    assert TOKEN not in repr(captured.value)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (SupervisorLogResponse(200, "application/json", b"{}"), "not text"),
        (SupervisorLogResponse(200, "text/plain", b"bad\x00text"), "invalid text"),
        (SupervisorLogResponse(200, "text/plain", b"\xff"), "not valid UTF-8"),
    ],
)
def test_rejects_malformed_or_ambiguous_log_responses(response, message) -> None:
    def transport(*args, **kwargs):
        del args, kwargs
        return response

    with pytest.raises(SupervisorLogError, match=message):
        collect_supervisor_logs(token=TOKEN, reference_time=REFERENCE, transport=transport)


def test_rejects_oversize_response_before_normalization() -> None:
    def transport(*args, **kwargs):
        del args, kwargs
        return SupervisorLogResponse(200, "text/plain", b"x" * 33)

    with pytest.raises(SupervisorLogError, match="exceeds size limit"):
        collect_supervisor_logs(
            token=TOKEN,
            reference_time=REFERENCE,
            max_response_bytes=32,
            transport=transport,
        )


def test_requires_timezone_aware_reference_time() -> None:
    with pytest.raises(SupervisorLogError, match="timezone-aware"):
        collect_supervisor_logs(token=TOKEN, reference_time=datetime(2026, 9, 10))
