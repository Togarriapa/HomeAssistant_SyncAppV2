"""Verified reversible atomic exchange for modified live Apply files."""

from __future__ import annotations

from .live_apply_atomic_replace import exchange_leaf, verify_displaced_leaf


class LiveApplyAtomicGuardError(RuntimeError):
    """A live leaf could not be exchanged while preserving its baseline safely."""


class LiveApplyAtomicBaselineMismatch(LiveApplyAtomicGuardError):
    """The displaced target differed from baseline and was safely restored."""


class LiveApplyAtomicOutcomeUncertain(LiveApplyAtomicGuardError):
    """The exchange could not be safely reversed, leaving mutation state uncertain."""


def exchange_verified_baseline(
    parent_fd: int,
    temporary: str,
    target: str,
    *,
    expected_object_id: str,
    expected_mode: str,
) -> None:
    """Exchange candidate into place only when the displaced leaf is the baseline.

    The first exchange preserves the exact target object under ``temporary``. If
    that displaced object is not the authorized baseline, the exchange is
    immediately reversed before reporting a deterministic pre-mutation block.
    A failed reversal is deliberately reported as an uncertain mutation outcome.
    """
    exchange_leaf(parent_fd, temporary, target)
    if verify_displaced_leaf(
        parent_fd,
        temporary,
        expected_object_id=expected_object_id,
        expected_mode=expected_mode,
    ):
        return
    try:
        exchange_leaf(parent_fd, temporary, target)
    except Exception as error:
        raise LiveApplyAtomicOutcomeUncertain(
            "live baseline changed and atomic exchange reversal is uncertain"
        ) from error
    raise LiveApplyAtomicBaselineMismatch("live baseline changed; atomic exchange was reversed")
