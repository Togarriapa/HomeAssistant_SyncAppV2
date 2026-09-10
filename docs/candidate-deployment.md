# Candidate deployment boundary

The sole product specification for candidate handling is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Trusted detection and durable work

`observe_trusted_candidate()` re-proves the configured private Repo B identity through the pinned repository-ID trust gate and inspects only the exact `candidate` branch. The observation binds Repo B target, stable GitHub repository ID, branch name `candidate`, and the exact candidate commit SHA, or explicit trusted branch absence. Repository, authentication, rate-limit and transport failures remain failures rather than candidate absence.

`detect_and_enqueue_trusted_candidate()` requires that pinned repository identity from protected `StateStore`. A present candidate is durably represented by work kind `candidate` keyed by the exact trusted commit SHA. Repeated observation of the same SHA is idempotent; a later SHA is separate work. Durable work records evidence only and grants no deployment authority.

`classify_candidate()` remains a deterministic comparison helper returning `absent`, `new`, or `unchanged` against an explicitly supplied prior SHA.

## Isolated Fetch boundary

`fetch_trusted_candidate()` implements only the next **Fetch** step. It accepts one present trusted candidate observation plus the exact expected SHA, an owner-only workspace root, and the live Home Assistant root used solely to prove that Git metadata will be created outside that live tree.

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

## Not implemented yet

The fetched Git object has not yet been materialized into a candidate staging tree. No remote bytes are copied into the live Home Assistant configuration. Dependency analysis, risk classification, Home Assistant configuration validation, pre-deployment backup, apply, reload/restart, observation, promotion, rejection marking and rollback are also not implemented by this boundary.

The next Remote -> Home Assistant increment should materialize the exact fetched commit into separate protected staging without symlink/path escape, generate deterministic file/integrity evidence and compare it with the relevant known-good baseline. Only after that staging boundary is independently verified should dependency analysis, risk classification and Home Assistant validation be introduced. Apply must remain downstream of validation and a recoverable pre-deployment backup, followed by observation and automatic rollback on failure.
