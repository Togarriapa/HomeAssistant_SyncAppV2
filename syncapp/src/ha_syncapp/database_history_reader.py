from __future__ import annotations

import json
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ha_syncapp.database_history_evidence import (
    DatabaseHistoryEvidenceError,
    DatabaseHistoryRecord,
    TrustedDatabaseHistoryEvidence,
    validate_trusted_database_history_evidence,
)
from ha_syncapp.database_retention import DATABASE_BRANCH, MAX_DATABASE_SNAPSHOTS
from ha_syncapp.github_repo import (
    RepositoryVerificationError,
    fetch_trusted_branch_head,
)

_HISTORY_PAGE_SIZE = 100
_MAX_HISTORY_PAGE_BYTES = 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 10.0


class DatabaseHistoryReadError(RuntimeError):
    """Trusted Recorder database history could not be collected safely."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def fetch_trusted_database_history_evidence(
    *,
    target: str,
    token: str,
    expected_id: int,
    reference_time: datetime,
    retention_days: int,
) -> TrustedDatabaseHistoryEvidence:
    """Collect complete read-only database history bound to one verified Repo B head."""

    try:
        branch_head = fetch_trusted_branch_head(
            target,
            token,
            expected_id=expected_id,
            branch=DATABASE_BRANCH,
        )
    except RepositoryVerificationError:
        raise DatabaseHistoryReadError("trusted database repository verification failed") from None

    records = _fetch_history_records(branch_head.target, token, head_sha=branch_head.commit_sha)
    try:
        return validate_trusted_database_history_evidence(
            branch_head=branch_head,
            records=records,
            reference_time=reference_time,
            retention_days=retention_days,
        )
    except DatabaseHistoryEvidenceError as error:
        raise DatabaseHistoryReadError(str(error)) from None


def _fetch_history_records(
    target: str,
    token: str,
    *,
    head_sha: str,
) -> tuple[DatabaseHistoryRecord, ...]:
    """Read commit metadata from the immutable verified head through the history root."""

    owner, repository = _target_parts(target)
    base_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repository, safe='')}/commits"
    )
    records: list[DatabaseHistoryRecord] = []
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
        if len(records) + len(parsed) > MAX_DATABASE_SNAPSHOTS:
            raise DatabaseHistoryReadError("database history exceeds the evidence limit")
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
        raise DatabaseHistoryReadError("trusted database repository target is invalid")
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
        raise DatabaseHistoryReadError(
            f"GitHub database history read failed with HTTP {error.code}"
        ) from None
    except (URLError, TimeoutError, OSError):
        raise DatabaseHistoryReadError("GitHub database history transport failed") from None

    if len(raw) > _MAX_HISTORY_PAGE_BYTES:
        raise DatabaseHistoryReadError("GitHub database history page exceeded the size limit")
    try:
        parsed: object = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        return parsed
    except (UnicodeError, ValueError, RecursionError):
        raise DatabaseHistoryReadError(
            "GitHub returned invalid database history metadata"
        ) from None


def _parse_history_page(page: object) -> tuple[DatabaseHistoryRecord, ...]:
    if not isinstance(page, list):
        raise DatabaseHistoryReadError("GitHub returned invalid database history metadata")

    records: list[DatabaseHistoryRecord] = []
    for item in page:
        if not isinstance(item, dict):
            raise DatabaseHistoryReadError("GitHub returned invalid database history metadata")
        sha = item.get("sha")
        commit = item.get("commit")
        parents = item.get("parents")
        if (
            not isinstance(sha, str)
            or not isinstance(commit, dict)
            or not isinstance(parents, list)
        ):
            raise DatabaseHistoryReadError("GitHub returned invalid database history metadata")

        committer = commit.get("committer")
        if not isinstance(committer, dict):
            raise DatabaseHistoryReadError("GitHub returned invalid database history metadata")
        committed_at = _parse_timestamp(committer.get("date"))

        parent_shas: list[str] = []
        for parent in parents:
            if not isinstance(parent, dict):
                raise DatabaseHistoryReadError("GitHub returned invalid database history metadata")
            parent_sha = parent.get("sha")
            if not isinstance(parent_sha, str):
                raise DatabaseHistoryReadError("GitHub returned invalid database history metadata")
            parent_shas.append(parent_sha)

        records.append(
            DatabaseHistoryRecord(
                sha=sha,
                committed_at=committed_at,
                parent_shas=tuple(parent_shas),
            )
        )
    return tuple(records)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise DatabaseHistoryReadError("GitHub returned invalid database history metadata")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise DatabaseHistoryReadError(
            "GitHub returned invalid database history metadata"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatabaseHistoryReadError("GitHub returned invalid database history metadata")
    return parsed
