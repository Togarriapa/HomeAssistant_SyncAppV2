# Trusted logs history evidence

This increment is derived only from the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947` and continues issue #222.

The previous retention planner classifies generated `logs` history against the fixed 30-day policy but deliberately accepts explicit evidence rather than reading repository state. Before any future history replacement can be considered, that evidence must be bound to the exact private Repo B identity and exact `logs` head already proven by the existing trusted branch-head verifier.

## This increment

`ha_syncapp.log_history_evidence` is still side-effect free. It accepts an already verified `BranchHead`, complete commit records, and an explicit UTC reference time. It then:

- accepts only the exact `logs` branch;
- requires a valid positive Repo B identity and exact 40-character current head;
- requires the first history record to equal the verified remote head;
- requires a complete linear single-parent chain all the way to the root commit;
- rejects merge history, broken parent links, truncated evidence, malformed identities, and oversized input;
- delegates duplicate, timestamp, ordering, future-time, cutoff, and retention classification to the already-green retention planner;
- returns one immutable evidence object containing the bound Repo B identity, expected head, validated commits, and retention plan.

## Read-only collection

`ha_syncapp.log_history_reader.fetch_trusted_log_history_evidence()` collects the evidence from the private Repo B without mutating Git. It first re-proves the pinned repository identity and exact `logs` head, then requests commit metadata by that immutable SHA rather than by a moving branch name. Pagination is limited to 100 commits per response, each response is limited to 1 MiB, and a complete history is limited to 4,096 commits. Incomplete, nonlinear, malformed, oversized, or divergent evidence fails closed with sanitized diagnostics that never include the GitHub token.

This read boundary grants no ref-update or history-rewrite authority. A later mutation increment must re-prove both repository identity and the exact remote head immediately before replacement and fail closed on any divergence.

## Immediate prewrite re-proof

`ha_syncapp.log_history_prewrite.reprove_log_history_prewrite()` is the final read-only guard before any future history mutation. It accepts only validated `logs` evidence, invokes the trusted branch verifier with the pinned repository ID, and requires the fresh target, repository ID, branch, and head SHA to match the evidence exactly. A moved head, changed repository identity, wrong branch, inconsistent evidence, or repository-verification failure blocks replacement with a sanitized error.

The returned proof is evidence only; it cannot push, force-update a ref, rewrite history, or mutate Home Assistant. A future writer must consume the proof immediately and remain responsible for an atomic expected-head-bound replacement operation.

## Replacement authorization boundary

`ha_syncapp.log_history_replacement.authorize_log_history_replacement()` binds the validated retention plan to that fresh prewrite proof without performing any repository operation. It accepts only the exact `logs` branch, requires byte-exact target plus repository/head equality between the two trusted inputs, recalculates the deterministic plan from the trusted commit evidence, verifies that the retained and pruned commit identities form the exact validated history partition, and keeps the current head as the first retained commit.

The returned immutable authorization explicitly reports whether replacement is required. An empty pruned set is a no-op and grants no mutation authority. Revalidation uses the plan's explicit reference time and cannot choose a new cutoff. The authorization does not inspect repository state, execute Git, contact GitHub, or mutate Home Assistant. A later transport must consume this exact authorization together with a still-fresh expected-head guarantee and remain restricted to `logs`.

## Expected-head replacement transport

`ha_syncapp.log_history_replace_transport.replace_logs_history()` is the narrow final mutation boundary. It performs no operation for a no-op authorization. Otherwise it validates the exact authorized private repository target and numeric identity, accepts only a distinct Git SHA using the same object format as the expected head, and addresses the authorized repository URL directly instead of trusting a mutable local `origin` alias. The only writable ref in its command is `refs/heads/logs`, protected by `--force-with-lease=refs/heads/logs:<expected-head>` so concurrent remote movement rejects the replacement.

The transport uses an absolute Git executable, disables repository hooks and credential-helper configuration, rejects symlink or non-directory workspaces, bounds execution time to five minutes, and sanitizes all process and lease failures. It does not build replacement commits, select retained identities, or reach candidate deployment and Home Assistant mutation paths.

## Safety boundary

Only the final expected-head-guarded `logs` ref replacement can mutate Git history. This capability performs no Git fetch or clone, Home Assistant mutation, candidate Fetch/Stage, validation execution, backup, Apply, reload/restart, observation, promotion, tagging, or rollback.

History rewriting remains retention management, not forensic secure deletion. The separate Retrigger Work Cron Job remains enabled, independent, and unchanged.
