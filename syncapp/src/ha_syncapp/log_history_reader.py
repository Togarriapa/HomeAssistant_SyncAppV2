from __future__ import annotations

import json
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ha_syncapp.github_repo import (
    RepositoryVerificationError,
    fetch_trusted_branch_head,
)
from ha_syncapp.log_history_evidence import (
    LogHistoryEvidenceError,
    LogHistoryRecord,
    TrustedLogHistoryEvidence,
    validate_trusted_log_history_evidence,
)
from ha_syncapp.log_history_retention import LOG_HISTORY_BRANCH, MAX_HISTORY_COMMITS

_HISTORY_PAGE_SIZE = 100
_MAX_HISTORY_PAGE_BYTES = 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 10.0


class LogHistoryReadError(RuntimeError):
    """Trusted logs history could not be collected safely."""


def fetch_trusted_log_history_evidence(
    *,
    target: str,
    token: str,
    expected_id: int,
    reference_time: datetime,
) -> TrustedLogHistoryEvidence:
    """Collect complete read-only logs history bound to one verified Repo B head."""

    try:
        branch_head = fetch_trusted_branch_head(
            target,
            token,
            expected_id=expected_id,
            branch=LOG_HISTORY_BRANCH,
        )
    except RepositoryVerificationError:
        raise LogHistoryReadError("trusted logs repository verification failed") from None

    records = _fetch_history_records(branch_head.target, token, head_sha=branch_head.commit_sha)
    try:
        return validate_trusted_log_history_evidence(
            branch_head=branch_head,
            records=records,
            reference_time=reference_time,
        )
    except LogHistoryEvidenceError as error:
        raise LogHistoryReadError(str(error)) from None


def _fetch_history_records(
    target: str,
    token: str,
    *,
    head_sha: str,
) -> tuple[LogHistoryRecord, ...]:
    """Read commit metadata from the immutable verified head through the history root."""

    owner, repository = _target_parts(target)
    base_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/commits"
    )
    records: list[LogHistoryRecord] = []
    page_number = 1

    while True:
        query = urlencode(
            {
                "sha": head_sha,
                "per_page": _HISTORY_PAGE_SIZE,
                "page": page_number,
            }
        )
        page = _read_history_page(_request(f"{base_url}?{query}", token))
        parsed = _parse_history_page(page)
        if not parsed:
            break
        if len(records) + len(parsed) > MAX_HISTORY_COMMITS:
            raise LogHistoryReadError("logs history exceeds the evidence limit")
        records.extend(parsed)

        if not parsed[-1].parent_shas:
            break
        if len(parsed) < _HISTORY_PAGE_SIZE:
            break
        page_number += 1

    return tuple(records)


def _target_parts(target: str) -> tuple[str, str]:
    parts = target.split("/")
    if len(parts) != 2 or not all(parts):
        raise LogHistoryReadError("trusted logs repository target is invalid")
    return parts[0], parts[1]


def _request(url: str, token: str) -> Request:
    return Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "HomeAssistant-SyncAppV2",
        },
        method="GET",
    )


def _read_history_page(request: Request) -> object:
    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            raw = response.read(_MAX_HISTORY_PAGE_BYTES + 1)
    except HTTPError as error:
        raise LogHistoryReadError(
            f"GitHub logs history read failed with HTTP {error.code}"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise LogHistoryReadError("GitHub logs history transport failed") from None

    if len(raw) > _MAX_HISTORY_PAGE_BYTES:
        raise LogHistoryReadError("GitHub logs history page exceeded the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise LogHistoryReadError("GitHub returned invalid logs history metadata") from None


def _parse_history_page(page: object) -> tuple[LogHistoryRecord, ...]:
    if not isinstance(page, list):
        raise LogHistoryReadError("GitHub returned invalid logs history metadata")

    records: list[LogHistoryRecord] = []
    for item in page:
        if not isinstance(item, dict):
            raise LogHistoryReadError("GitHub returned invalid logs history metadata")
        sha = item.get("sha")
        commit = item.get("commit")
        parents = item.get("parents")
        if not isinstance(sha, str) or not isinstance(commit, dict) or not isinstance(parents, list):
            raise LogHistoryReadError("GitHub returned invalid logs history metadata")

        committer = commit.get("committer")
        if not isinstance(committer, dict):
            raise LogHistoryReadError("GitHub returned invalid logs history metadata")
        committed_at = _parse_timestamp(committer.get("date"))

        parent_shas: list[str] = []
        for parent in parents:
            if not isinstance(parent, dict) or not isinstance(parent.get("sha"), str):
                raise LogHistoryReadError("GitHub returned invalid logs history metadata")
            parent_shas.append(parent["sha"])

        records.append(
            LogHistoryRecord(
                sha=sha,
                committed_at=committed_at,
                parent_shas=tuple(parent_shas),
            )
        )
    return tuple(records)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise LogHistoryReadError("GitHub returned invalid logs history metadata")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise LogHistoryReadError("GitHub returned invalid logs history metadata") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LogHistoryReadError("GitHub returned invalid logs history metadata")
    return parsed
