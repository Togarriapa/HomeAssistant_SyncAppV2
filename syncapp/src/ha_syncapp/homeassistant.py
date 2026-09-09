"""Supervisor and Core adapters with explicit response/health checks."""

from typing import Any

from .errors import Failure
from .http import HttpClient


class HomeAssistant:
    def __init__(self, token: str) -> None:
        if not token:
            raise Failure("supervisor_token_missing")
        self.client = HttpClient("http://supervisor", token)

    def supervisor(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = self.client.request(method, path, body)
        if not isinstance(result, dict) or result.get("result") != "ok":
            raise Failure("supervisor_operation_failed", retryable=True)
        data = result.get("data")
        if not isinstance(data, dict):
            raise Failure("supervisor_response_invalid")
        return data

    def core(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.client.request(method, "/core/api" + path, body)

    def health(self) -> dict[str, Any]:
        supervisor = self.supervisor("GET", "/supervisor/info")
        if supervisor.get("healthy") is not True:
            raise Failure("home_assistant_unhealthy", retryable=True)
        info = self.supervisor("GET", "/core/info")
        config = self.core("GET", "/config")
        if (
            info.get("state") != "started"
            or not isinstance(config, dict)
            or config.get("state") != "RUNNING"
            or not isinstance(config.get("version"), str)
            or config["version"] != info.get("version")
        ):
            raise Failure("home_assistant_unhealthy", retryable=True)
        return config
