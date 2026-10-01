# Deploy-key operator controls

SyncApp exposes the protected Repo B deploy-key lifecycle as explicit, one-shot
Home Assistant App options. The control plane never enrolls or removes a GitHub deploy key.
Manual GitHub administration remains a deliberate trust boundary.

## Preconditions

Configure `repo_b` and `github_token` first. The token is used to re-prove the
private repository's numeric identity; it is never passed to Git transport when a
deploy-key primitive runs. Configure both `repo_b_admin_action` and
`repo_b_admin_request_id`, using a canonical lowercase UUIDv4.

A request receipt is bound to the action, case-insensitive repository target, and
numeric repository identity. Reusing that request/action for a different repository
fails closed. The receipt stores no token, private key, source path, Git reference,
or commit SHA.

## Enrollment and access test

Use this sequence: generate → manually enroll → test.

1. Select `generate` with a new request UUID and restart the App. Copy the public
   key from the `repo_b_admin_completed` log event. The private key remains under
   protected App storage.
2. In the exact configured private GitHub repository, add that public key as a
   deploy key with write access. SyncApp does not call the GitHub deploy-key
   administration API.
3. Select `test` with a new request UUID and restart. Success means the protected
   key authenticated over strict host-pinned SSH and the observed repository still
   matches the pinned numeric identity.

Generation alone is not proof of enrollment or write permission. A failed access
test cannot enable any deploy-key transport selector.

## Rotation

Use this sequence: rotate_prepare → manually enroll → rotate_verify → rotate_activate.

Use the same request UUID for all three rotation actions. `rotate_prepare` returns
the candidate public key. Add it to GitHub while retaining the old deploy key, then
run `rotate_verify`. Only a verified candidate can be selected by `rotate_activate`.
Activation retains the prior local generation for compatibility rollback; it does
not remove either GitHub key. Remove an old GitHub key only after all selected lanes
have been observed working with the replacement.

## Empty repository initialization

After `test` succeeds, select `initialize` with a new request UUID only when Repo B
is intended to be empty. SyncApp obtains a fresh exact reference snapshot. Any
existing branch or tag, an existing local baseline, another active request, changed
source snapshot, changed key, or remote divergence blocks initialization.

The executor captures `/homeassistant` through the normal inclusion policy, creates
one deterministic initial `main` commit, and publishes it through the verified key
without force. It journals preparation before mutation and reconciles an interrupted
publish against the exact remote commit before deciding whether to retry or block.

## Replay, retry, and diagnostics

A completed or deterministically blocked request/action is not executed again while
the same options remain configured. Transient network and API failures are recorded
as `retry`, with exponential delays starting at 60 seconds, capped at one hour, and
an eight-attempt ceiling. A new request UUID is required after correcting a blocked
condition; rotation stages are the exception and intentionally share their one
rotation UUID.

Generated runtime inventory reports action/status counts, attempt totals, the next
retry timestamp, and the latest processing timestamp. It omits request IDs,
repository identity, fingerprints, key material, references, paths, and commits.

The control plane does not bypass validation, backup, observation, or rollback. Transport
selectors remain independent and default to `token`; choosing `deploy_key` still
requires an initialized baseline and a fresh identity-bound access proof.
