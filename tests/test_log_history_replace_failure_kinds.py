from __future__ import annotations

from ha_syncapp.log_history_replace_transport import (
    LogHistoryReplacementFailureKind,
    LogHistoryReplacementTransportError,
)


def test_only_transient_log_replacement_failures_are_retryable() -> None:
    for kind in LogHistoryReplacementFailureKind:
        error = LogHistoryReplacementTransportError("sanitized", kind=kind)
        assert error.retryable is (kind is LogHistoryReplacementFailureKind.TRANSIENT)


def test_log_replacement_failure_kinds_are_stable_runtime_values() -> None:
    assert {kind.value for kind in LogHistoryReplacementFailureKind} == {
        "invalid",
        "stale",
        "transient",
        "rejected",
    }
