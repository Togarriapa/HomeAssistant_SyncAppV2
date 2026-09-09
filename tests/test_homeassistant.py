from unittest.mock import Mock

import pytest
from ha_syncapp.errors import Failure
from ha_syncapp.homeassistant import HomeAssistant


def test_health_requires_supervisor_and_live_core() -> None:
    api = HomeAssistant("sentinel")
    api.client = Mock()
    api.client.request.side_effect = [
        {"result": "ok", "data": {"healthy": True}},
        {"result": "ok", "data": {"state": "started", "version": "2026.9.1"}},
        {"version": "2026.9.1", "state": "RUNNING"},
    ]
    assert api.health()["version"] == "2026.9.1"
    api.client.request.side_effect = [{"result": "ok", "data": {"healthy": False}}]
    with pytest.raises(Failure, match="home_assistant_unhealthy"):
        api.health()


def test_supervisor_error_envelopes_are_not_success() -> None:
    api = HomeAssistant("sentinel")
    api.client = Mock()
    api.client.request.return_value = {"result": "error", "message": "secret"}
    with pytest.raises(Failure, match="supervisor_operation_failed"):
        api.supervisor("GET", "/core/info")
