"""Bounded authenticated HTTP with fixed origins and no credential-bearing redirects."""

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .errors import Failure
from .journal import Journal


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise Failure("api_redirect_refused")


class HttpClient:
    def __init__(self, origin: str, token: str, *, timeout: int = 30) -> None:
        self.origin = origin.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        raw: bool = False,
        limit: int = 32 * 1024 * 1024,
    ) -> Any:
        if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n"):
            raise Failure("invalid_api_path")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": "HomeAssistant-SyncAppV2",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.origin + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(limit + 1)
            if len(content) > limit:
                raise Failure("api_response_too_large")
            if raw:
                return content
            return json.loads(content) if content else None
        except urllib.error.HTTPError as error:
            retry = (
                error.code in (408, 425, 429)
                or error.code >= 500
                or (
                    error.code == 403
                    and (
                        error.headers.get("Retry-After")
                        or error.headers.get("X-RateLimit-Remaining") == "0"
                    )
                )
            )
            raise Failure(f"api_http_{error.code}", retryable=bool(retry)) from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise Failure("api_unavailable", retryable=True) from None
        except (ValueError, UnicodeError):
            raise Failure("api_invalid_response") from None


class GitHubGuard:
    def __init__(self, repository: str, token: str, journal: Journal) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository):
            raise Failure("invalid_repository")
        if repository.split("/")[1] in (".", ".."):
            raise Failure("invalid_repository")
        self.repository = repository
        self.journal = journal
        self.client = HttpClient("https://api.github.com", token)

    @property
    def remote(self) -> str:
        return f"git@github.com:{self.repository}.git"

    def verify(self) -> None:
        info = self.client.request("GET", f"/repos/{self.repository}")
        if (
            not isinstance(info, dict)
            or info.get("private") is not True
            or info.get("visibility", "private") != "private"
            or info.get("archived") is not False
        ):
            raise Failure("repository_not_private")
        if (
            str(info.get("full_name", "")).lower() != self.repository.lower()
            or type(info.get("id")) is not int
            or info["id"] <= 0
        ):
            raise Failure("repository_identity_changed")
        bound = self.journal.value("repository_id")
        previous_bindings = self.journal.db.execute(
            "SELECT repository_id FROM repository_binding WHERE lower(target) = lower(?)",
            (self.repository,),
        ).fetchall()
        if any(row[0] != info["id"] for row in previous_bindings):
            raise Failure("repository_identity_changed")
        if bound is not None and bound != info["id"]:
            raise Failure("repository_identity_changed")
        if bound is None:
            self.journal.set("repository_id", info["id"])
