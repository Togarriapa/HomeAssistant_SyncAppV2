# Candidate Fetch/Stage checkpoint and recovery

Schema v27 executes the first controlled candidate deployment action. It accepts
only a claimed `candidate` work item whose immutable orchestration state is exactly
`detected/fetch_stage`, re-proves the pinned private repository and exact candidate
SHA, fetches into an isolated Git workspace, and materializes a verified Stage
outside the live Home Assistant tree.

## Durable authority and crash windows

Before any network or filesystem action, the worker persists a `planned` checkpoint
bound to the exact orchestration digest, repository identity and candidate SHA. The
checkpoint contains an opaque UUID workspace identity, never a filesystem path.
After staging, the worker verifies every manifest and tree byte, atomically renames
the Stage to its UUID-derived durable location, fsyncs the Stage and parent directory,
and atomically commits both the `completed` checkpoint and the orchestration
transition to `staged/analyze`.

| Durable state | Safe recovery behavior |
|---|---|
| no checkpoint | Persist intent before starting Fetch/Stage. |
| `planned`, no durable Stage | Remove only owned incomplete artifacts, then re-prove and retry the exact candidate. |
| `planned`, incomplete UUID Stage | Remove the bounded owned directory, then retry from fresh evidence. |
| `completed` and `staged/analyze` | Reconstruct and verify the durable Stage without credentials or network access. |
| missing, malformed, rebound or tampered evidence | Fail deterministically and block the exact unchanged candidate. |

The persisted manifest is bounded to 16 MiB and 4,096 entries. Reconstruction
rejects duplicate JSON keys, unsafe paths, links, unexpected files, identity drift,
noncanonical order, digest mismatch and unsafe ownership or modes. Temporary cleanup
is bounded to 64 owner-controlled Stage directories and never follows symlinks.

## Retrigger behavior

The Fetch/Stage Retrigger lane recovers interrupted `candidate` claims and atomically
claims at most one due item whose next action is `fetch_stage`. A successful action
returns the work item to `pending` with a reset attempt count so the later Analyze
lane can claim it in a future cycle. Transient transport or filesystem failures use
the existing bounded exponential backoff. Deterministic evidence failures block the
exact SHA and cannot loop until an explicit retry or changed candidate creates new
authority.

The lane runs after rollback recovery and before new candidate detection. Therefore
a newly detected candidate cannot be fetched in the same cycle, and a cycle performs
at most one candidate Fetch/Stage action. Workspaces are private `0700` roots derived
outside Home Assistant; the Git fetch workspace is removed after every attempt.

## Runtime and safety boundary

Runtime inventory exposes only checkpoint phase counts, aggregate staged entry/byte
counts and the latest checkpoint time. It omits candidate SHA, repository target and
ID, workspace identity/path, token, manifest content and nested errors.

Fetch/Stage does not read or write live Home Assistant configuration. It grants no
Analyze, validation, backup, Apply, restart, observation, promotion, tagging or
rollback authority. Those remain separate evidence-gated actions.
