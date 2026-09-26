from __future__ import annotations

from datetime import UTC, datetime

import pytest


NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def test_integrity_execution_module_defines_crash_safe_entrypoint() -> None:
    from ha_syncapp.candidate_integrity_execution import (
        execute_candidate_integrity_once,
        load_candidate_integrity_checkpoint,
    )

    assert callable(execute_candidate_integrity_once)
    assert callable(load_candidate_integrity_checkpoint)


def test_placeholder_marks_red_contract_as_incomplete() -> None:
    with pytest.raises(NotImplementedError):
        raise NotImplementedError("RED: schema-v28 integrity execution is not implemented")
