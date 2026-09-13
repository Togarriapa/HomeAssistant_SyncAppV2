from __future__ import annotations

import pytest
from ha_syncapp.database_history_replace_transport import (
    DatabaseHistoryReplacementFailureKind,
    DatabaseHistoryReplacementTransportError,
    _repository_verification_failure_kind,
)
from ha_syncapp.github_repo import RepositoryVerificationError


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "GitHub repository verification transport failed",
            DatabaseHistoryReplacementFailureKind.TRANSIENT,
        ),
        (
            "GitHub repository verification failed with HTTP 429",
            DatabaseHistoryReplacementFailureKind.TRANSIENT,
        ),
        (
            "GitHub repository verification failed with HTTP 503",
            DatabaseHistoryReplacementFailureKind.TRANSIENT,
        ),
        (
            "GitHub repository verification failed with HTTP 401",
            DatabaseHistoryReplacementFailureKind.INVALID,
        ),
        (
            "Configured repository identity changed unexpectedly",
            DatabaseHistoryReplacementFailureKind.INVALID,
        ),
    ],
)
def test_repository_verification_failures_have_deterministic_retry_classification(
    message: str,
    expected: DatabaseHistoryReplacementFailureKind,
) -> None:
    error = RepositoryVerificationError(message)
    assert _repository_verification_failure_kind(error) is expected


def test_only_transient_database_replacement_failures_are_retryable() -> None:
    for kind in DatabaseHistoryReplacementFailureKind:
        error = DatabaseHistoryReplacementTransportError("sanitized", kind=kind)
        assert error.retryable is (
            kind is DatabaseHistoryReplacementFailureKind.TRANSIENT
        )
