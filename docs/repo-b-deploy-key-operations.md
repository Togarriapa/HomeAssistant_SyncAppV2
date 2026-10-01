# Repo B deploy-key operations

This is the authoritative operator runbook for enrolling, testing, initializing,
rolling out, rotating, recovering, rolling back compatibility, and eventually
removing Repo B deploy keys. The linked specialist documents remain the detailed
security and implementation evidence.

## Prerequisites and authority boundaries

- Configure the exact private `repo_b` as `owner/repository` and a `github_token`.
  GitHub REST identity verification uses the token to prove privacy and the stable
  numeric repository ID. A deploy key authenticates Git/SSH; it cannot replace
  the REST identity check, so the token remains required for REST operations.
- Keep the Home Assistant configuration mount read-only and preserve
  `/data/syncapp`. Never copy, display, back up separately, or edit the private key.
- In GitHub, enrollment and removal happen manually under **Settings → Deploy keys**.
  Select **Allow write access** because publication, promotion, initialization and
  retention are write-capable. SyncApp never changes the GitHub key list.
- Leave all five transport selectors at their `token` defaults until enrollment,
  access testing, and explicit initialization have succeeded.
- Back up the App and record the current transport settings before rollout. The
  recurring Retrigger remains enabled throughout this procedure.

The lifecycle controls are `repo_b_admin_action` and
`repo_b_admin_request_id`. Configure them together. Use a new canonical lowercase
UUIDv4 for each new `generate`, `test`, or `initialize` action. Use one shared UUID
for the related `rotate_prepare`, `rotate_verify`, and `rotate_activate` stages.
Keeping the same pair is a safe receipt replay, not a new request.

## First enrollment and initialization

1. Select `repo_b_admin_action: generate`, set a new request UUID, and restart the
   App. Copy only the public key, fingerprint, and generation ID from the sanitized
   `repo_b_admin_completed` event. Generation is defined in
   [deploy-key-generation.md](deploy-key-generation.md).
2. Add that public key to the exact private repository in **Settings → Deploy keys**
   and enable **Allow write access**. Do not expose or move the private key.
3. Select `test` with a new UUID and restart. The read-only, identity-bound,
   host-pinned proof in [deploy-key-access-test.md](deploy-key-access-test.md) must
   complete before any deploy-key transport is enabled.
4. For a new Repo B only, confirm it has no branches or tags, then select
   `initialize` with a new UUID. SyncApp takes a fresh complete ref snapshot,
   journals authority, snapshots the routed Home Assistant configuration, creates
   the deterministic first `main` commit, and publishes it without force through
   [repo-b-initialization-execution.md](repo-b-initialization-execution.md).
5. Confirm the initialization receipt and durable `main` baseline in logs/runtime
   inventory. A non-empty repository, existing baseline, concurrent authority,
   source change, key change, or remote divergence blocks the request.
6. Remove the two administrative action fields after recording the successful
   result. Leaving them present is safe because the durable receipt only replays.

Initialization is an explicit operator mutation. Routine startup and Retrigger
cannot invent an initialization request, bypass the empty-repository proof, or
silently fall back to token Git.

## Staged transport rollout

Change one selector at a time, restart, and complete the verification section
before advancing. This order moves from read-only observation to ordinary writes
and then to narrower, higher-impact mutation paths:

The exact selectors are `repo_b_candidate_transport`,
`repo_b_publication_transport`, `repo_b_promotion_transport`,
`repo_b_rollback_transport`, and `repo_b_retention_transport`.

| Stage | Option | Scope | Detailed evidence |
| --- | --- | --- | --- |
| 1 | `repo_b_candidate_transport: deploy_key` | Candidate observation and exact fetch/stage | [deploy-key-candidate-ingress-adoption.md](deploy-key-candidate-ingress-adoption.md) |
| 2 | `repo_b_publication_transport: deploy_key` | Local, Recorder, runtime and log snapshot publication | [deploy-key-snapshot-publication-adoption.md](deploy-key-snapshot-publication-adoption.md) |
| 3 | `repo_b_promotion_transport: deploy_key` | Atomic non-force `main` plus known-good tag promotion | [deploy-key-promotion-adoption.md](deploy-key-promotion-adoption.md) |
| 4 | `repo_b_rollback_transport: deploy_key` | Read-only repository proof during rollback; Supervisor still owns restore | [deploy-key-rollback-adoption.md](deploy-key-rollback-adoption.md) |
| 5 | `repo_b_retention_transport: deploy_key` | Exact-lease generated `logs` and `database` history rewrites | [deploy-key-retention-adoption.md](deploy-key-retention-adoption.md) |

Every selector defaults independently to `token`. A selected lane requires the
pinned repository identity, initialized `main` baseline, valid protected key, and
fresh access proof. It fails closed instead of using token Git as a hidden fallback.

## Verification

After every restart or selector change:

1. Confirm there is no `configuration_invalid`, `repo_b_untrusted`, or
   lane-specific authority failure.
2. Confirm startup obtained a fresh access proof for the active generation and the
   service reached its expected active/passive mode.
3. Inspect generated runtime/deployment information. Deploy-key administrative
   status is aggregate and content-free; initialization and recovery phases must
   show no unexpected `blocked`, `retry`, `uncertain`, or interrupted state.
4. Exercise only the normal event/cadence for the newly selected lane. Confirm its
   exact expected branch/tag changed and unrelated refs did not.
5. Confirm logs, artifacts and durable work contain no token, private key, private
   path, repository contents, raw command output, or unexpected ref/commit identity.
6. Keep the former credential enrolled until every required stage has operated and
   recovery evidence is clean across at least one App restart.

Selection never broadens authority: it does not bypass candidate validation, does
not bypass backup, does not bypass deployment observation, and does not bypass
rollback. Home Assistant/Supervisor authorization, locks, unique work identities,
non-force publication, exact leases, idempotency, and deterministic blocking remain
authoritative.

## Rotation

Follow [deploy-key-rotation.md](deploy-key-rotation.md) with one new rotation UUID:

1. Run `rotate_prepare`; copy the candidate public key from the completed event.
2. Manually enroll the candidate under **Settings → Deploy keys** with **Allow write
   access**. Keep the current GitHub deploy key enrolled.
3. Run `rotate_verify` with the same UUID. A transient network failure remains
   retryable; authentication, host-pin, identity, or proof mismatch blocks safely.
4. Run `rotate_activate` with the same UUID only after verification. Crash-safe
   directory reconciliation makes replay idempotent and the previous local
   generation is retained.
5. Restart and repeat staged verification for every selected lane before considering
   old GitHub-key removal.

Rotation never deletes the previous local generation or either GitHub key. In
particular, never delete the only working credential.

## Failure and recovery matrix

| Observation | Classification | Safe operator action |
| --- | --- | --- |
| Invalid/partial options or noncanonical UUID | Deterministic | Correct configuration; use a new UUID only for a genuinely new action. |
| Repository is public, renamed/rebound, or numeric ID changed | Deterministic trust failure | Stop; verify `repo_b` and ownership. Do not bypass or repin implicitly. |
| Missing/tampered key, unsafe modes/links, bad host pin, rejected authentication | Deterministic | Preserve state, correct the external cause, then issue the appropriate new explicit action. |
| DNS, route, timeout, connection reset/refusal, or transient GitHub API failure | Transient | Leave the same request configured. Durable exponential backoff starts at 60 seconds, caps at one hour, and blocks after eight attempts. |
| `prepared` rotation | Expected pause | Enroll the candidate manually, then run `rotate_verify` with the same UUID. |
| Interrupted rotation activation | Reconciliation | Replay `rotate_activate`; exact generation/phase evidence decides the safe next rename. |
| Initialization `retry` | Transient/reconcilable | Preserve source, state and key. The executor re-proves remote refs and exact commit before any retry. |
| Initialization or publication divergence | Deterministic | Do not force. Inspect Repo B and preserve the journal for analysis. |
| Candidate SHA already rejected or work is `blocked` | Deterministic work failure | Fix the cause or change the candidate. Only then use an explicit administrative retry for that exact work kind/key and a new request UUID. |
| Stale process lock after reboot/crash | Kernel-recoverable | Restart normally; never delete the lock file. The kernel releases ownership and SyncApp reconciles interrupted work. |

The deploy-key action receipt and the separate blocked-work administrative retry
are different authorities. Neither creates wildcard/bulk retry, changes a candidate
identity, bypasses retry ceilings, or bypasses validation and deployment safeguards.
All executions, replays, retries, deferrals and failures remain visible through
sanitized logs and runtime/deployment information.

## Compatibility rollback

Rollback transport configuration before touching credentials:

1. Change only the affected `repo_b_*_transport` selector from `deploy_key` to
   `token` and restart.
2. Verify the token-backed lane and its existing durable baseline/checkpoints.
3. Repeat one selector at a time if a broader rollback is required.
4. Keep both GitHub deploy keys and protected local generations while diagnosing.

Changing transport does not undo or skip candidate validation, backup, deployment
observation, promotion reconciliation, rollback reconciliation, or retention leases.
GitHub REST identity verification continues to use the token in both modes.

## Key removal

GitHub key removal is manual and last. Remove an old GitHub deploy key only after:

- the replacement passed `rotate_verify` and `rotate_activate`;
- every selected transport lane passed the verification checklist after restart;
- runtime/deployment information has no pending retry, uncertain mutation, or stale
  authority tied to the old generation; and
- token compatibility rollback has been tested or remains available.

Remove only the specifically identified old public key in **Settings → Deploy
keys**. Never remove the active key, never remove both keys together, and never
delete protected App state as cleanup. SyncApp intentionally has no automatic
GitHub key-removal or retained-generation cleanup authority.

## Detailed security references

- [deploy-key-generation.md](deploy-key-generation.md)
- [deploy-key-access-test.md](deploy-key-access-test.md)
- [deploy-key-rotation.md](deploy-key-rotation.md)
- [repo-b-initialization-execution.md](repo-b-initialization-execution.md)
- [deploy-key-candidate-ingress-adoption.md](deploy-key-candidate-ingress-adoption.md)
- [deploy-key-snapshot-publication-adoption.md](deploy-key-snapshot-publication-adoption.md)
- [deploy-key-promotion-adoption.md](deploy-key-promotion-adoption.md)
- [deploy-key-rollback-adoption.md](deploy-key-rollback-adoption.md)
- [deploy-key-retention-adoption.md](deploy-key-retention-adoption.md)
