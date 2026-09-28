from ha_syncapp.candidate_rollback_handoff_execution import execute_candidate_rollback_handoff_once


def test_candidate_rollback_handoff_executor_exists() -> None:
    assert callable(execute_candidate_rollback_handoff_once)
