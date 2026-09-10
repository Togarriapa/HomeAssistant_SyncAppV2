# Candidate deployment boundary

The sole product specification for candidate handling is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Trusted detection and durable work

`observe_trusted_candidate()` re-proves the configured private Repo B identity through the pinned repository-ID trust gate and inspects only the exact `candidate` branch. The observation binds Repo B target, stable GitHub repository ID, branch name `candidate`, and the exact candidate commit SHA, or explicit trusted branch absence. Repository, authentication, rate-limit and transport failures remain failures rather than candidate absence.

`detect_and_enqueue_trusted_candidate()` requires that pinned repository identity from protected `StateStore`. A present candidate is durably represented by work kind `candidate` keyed by the exact trusted commit SHA. Repeated observation of the same SHA is idempotent; a later SHA is separate work. Durable work records evidence only and grants no deployment authority.

`classify_candidate()` remains a deterministic comparison helper returning `absent`, `new`, or `unchanged` against an explicitly supplied prior SHA.

## Isolated Fetch boundary

`fetch_trusted_candidate()` implements only **Fetch**. It accepts one present trusted candidate observation plus the exact expected SHA, an owner-only workspace root, and the live Home Assistant root used solely to prove that Git metadata will be created outside that live tree.

The fetch boundary:

- resolves both roots before creating transient state and fails closed if either contains the other or they are equal;
- initializes Git only in a private transient workspace beneath the isolated workspace root;
- uses a credential-free Repo B HTTPS URL and a temporary non-interactive authentication helper;
- fetches only `refs/heads/candidate` into the private `refs/syncapp/candidate-fetch` ref using a normal non-force refspec;
- resolves that ref as a commit and requires its SHA to equal both the trusted observation and expected durable SHA;
- verifies the fetched object type is `commit`;
- removes the authentication helper on every path and removes incomplete staging on failure;
- leaves no checked-out candidate files in the fetch workspace.

Branch movement between detection and fetch therefore fails closed instead of silently substituting a newer proposal. Fetch success still grants no validation or deployment authority.

## Isolated Stage boundary

`stage_fetched_candidate()` implements only **Stage**. Before reading the tree it re-verifies the private fetched workspace, its SyncApp ref, object type and exact commit SHA. The staging root must be an owner-only directory that is disjoint from both the live Home Assistant tree and the fetched Git workspace.

Stage uses Git plumbing rather than checkout/worktree operations. It recursively enumerates the exact fetched commit and accepts only regular `100644` or `100755` blobs. Symlinks, submodules/non-blob entries, malformed metadata, path traversal, `.git` path components, duplicate paths and file/directory prefix collisions fail closed. Blob output is written with exclusive/no-follow semantics where supported, its exact Git-reported size is checked, and only the Git executable bit is preserved.

Before materialization, the required blob size is compared with current free space while preserving a reserve for metadata and recovery. The resulting candidate tree is then passed through the existing descriptor-aware snapshot primitive, which hashes every file and re-verifies source stability. Stage adds a canonical `candidate.json` manifest that binds the snapshot integrity ID to Repo B target, stable repository ID and exact candidate commit SHA. `verify_candidate_stage()` re-hashes the staged tree, verifies the canonical manifest and its digest, and rejects unexpected root entries or tampering.

Temporary materialization is removed on every path. A failed Stage removes any incomplete snapshot. A successful Stage remains isolated evidence only; the durable candidate work item is not marked successful by this slice.

## Not implemented yet

No remote candidate bytes are copied into the live Home Assistant configuration. Dependency analysis, risk classification, Home Assistant configuration validation, pre-deployment backup, apply, reload/restart, observation, promotion, rejection marking and rollback are not authorized by Detect, Fetch or Stage.

The next Remote -> Home Assistant increment should compare the verified candidate stage with the relevant known-good baseline and build dependency/risk evidence without applying it. Home Assistant validation must then run against isolated staged configuration. Apply must remain downstream of successful validation and a recoverable pre-deployment backup, followed by observation and automatic rollback on failure.
