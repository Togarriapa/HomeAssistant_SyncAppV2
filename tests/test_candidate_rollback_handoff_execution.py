from __future__ import annotations

from ha_syncapp.candidate_rollback_handoff_execution import (
    execute_candidate_rollback_handoff_once,
)


def test_candidate_rollback_handoff_executor_is_available() -> None:
    assert callable(execute_candidate_rollback_handoff_once)
