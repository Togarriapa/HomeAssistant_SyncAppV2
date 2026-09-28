from __future__ import annotations

import pytest


def test_startup_error_recovery_module_exists() -> None:
    from ha_syncapp.candidate_startup_error_observation_execution import (
        execute_candidate_startup_error_observation_once,
    )

    assert callable(execute_candidate_startup_error_observation_once)
