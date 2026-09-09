"""Crash-safe verified orchestration of one authorized Local -> Repo B publication."""

from datetime import datetime

from ha_syncapp.git_workspace import GitWorkspace
from ha_syncapp.github_repo import (
    RepositoryVerificationError,
    fetch_optional_trusted_branch_head,
    fetch_trusted_branch_head,
)
from ha_syncapp.publication_intent import PublicationIntent
from ha_syncapp.publication_result import PublicationResultError, verify_publication_result
from ha_syncapp.publication_state import PublicationStateError, record_verified_publication
from ha_syncapp.publication_transport import PublicationTransportError, push_publication_intent
from ha_syncapp.state import StateStore, SynchronizationBaseline


class PublicationWorkflowError(RuntimeError):
    """An authorized publication did not complete every verification gate."""


def complete_authorized_publication(
    store: StateStore,
    workspace: GitWorkspace,
    intent: PublicationIntent,
    token: str,
    *,
    synchronized_at: datetime | None = None,
) -> SynchronizationBaseline:
    """Publish, verify the fresh remote result, then persist the proven baseline."""
    if type(store) is not StateStore:
        raise PublicationWorkflowError("publication workflow state store is invalid")
    if type(intent) is not PublicationIntent:
        raise PublicationWorkflowError("publication workflow intent evidence is invalid")

    try:
        before = fetch_optional_trusted_branch_head(
            intent.target,
            token,
            expected_id=intent.repository_id,
            branch=intent.branch,
        )
        pushed_commit = push_publication_intent(workspace, intent, before, token)
        if pushed_commit != intent.local_commit_sha:
            raise PublicationWorkflowError(
                "publication transport returned unexpected commit evidence"
            )

        after = fetch_trusted_branch_head(
            intent.target,
            token,
            expected_id=intent.repository_id,
            branch=intent.branch,
        )
        result = verify_publication_result(intent, after)
        return record_verified_publication(
            store,
            workspace,
            intent,
            result,
            synchronized_at=synchronized_at,
        )
    except PublicationWorkflowError:
        raise
    except (
        RepositoryVerificationError,
        PublicationTransportError,
        PublicationResultError,
        PublicationStateError,
    ) as exc:
        raise PublicationWorkflowError(
            "publication workflow did not complete every verification gate"
        ) from exc
