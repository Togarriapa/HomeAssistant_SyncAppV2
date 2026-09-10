# Candidate deployment boundary

The sole product specification for candidate handling is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Trusted detection and durable work

`observe_trusted_candidate()` re-proves the configured private Repo B identity through the pinned repository-ID trust gate and inspects only the exact `candidate` branch. The observation binds Repo B target, stable GitHub repository ID, branch name `candidate`, and the exact candidate commit SHA, or explicit trusted branch absence. Repository, authentication, rate-limit and transport failures remain failures rather than candidate absence.

`detect_and_enqueue_trusted_candidate()` requires that pinned repository identity from protected `StateStore`. A present candidate is durably represented by work kind `candidate` keyed by the exact trusted commit SHA. Repeated observation of the same SHA is idempotent; a later SHA is separate work. Durable work records evidence only and grants no deployment authority.

`classify_candidate()` remains a deterministic comparison helper returning `absent`, `new`, or `unchanged` against an explicitly supplied prior SHA.

## Isolated Fetch boundary

`fetch_trusted_candidate()` implements only the **Fetch** step. It accepts one present trusted candidate observation plus the exact expected SHA, an owner-only workspace root, and the live Home Assistant root used solely to prove that Git metadata will be created outside that live tree.

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

## Protected Stage boundary

`stage_fetched_candidate()` implements only the next **Stage** step. It consumes immutable successful Fetch evidence, re-proves the private candidate ref and exact commit before materialization, and writes candidate bytes only into a separate owner-only staging tree that is proven disjoint from the live Home Assistant root. A private app workspace may safely contain the fetched and staged workspaces as siblings; staging itself may never be created inside the fetched Git workspace.

The Stage boundary:

- enumerates the exact fetched commit with Git plumbing rather than checking out a branch;
- accepts only regular Git blob modes `100644` and `100755`;
- rejects symlinks, submodules, non-blob entries, malformed object IDs, duplicate/prefix-conflicting paths, absolute or traversal paths, empty path components, and every `.git` path component;
- materializes regular files under a newly-created private staging root, retaining only Git's executable-bit distinction while keeping group/other permissions closed;
- hashes every staged file with SHA-256 and writes a canonical manifest bound to Repo B target, stable repository ID, branch `candidate`, exact commit SHA, Git mode and Git object ID;
- validates the immutable evidence structure and canonical path ordering before later consumers can trust it;
- rescans and re-hashes the entire staged file and directory set before returning success;
- opens staged files and the manifest without following symlinks where supported and rejects hard-linked replacements;
- rejects inserted empty directories, unexpected files, modified modes, changed bytes, altered manifests and unexpected staging-root entries;
- re-proves the fetched candidate ref after materialization so staging cannot silently cross an immutable-evidence boundary;
- removes an incomplete staging tree on any failure.

`verify_candidate_stage()` lets every later validation step re-bind itself to the exact accepted manifest and staged bytes before consuming them. Stage success still grants no authority to modify Home Assistant.

## Not implemented yet

No candidate byte from Stage is copied into the live Home Assistant configuration. Dependency analysis, risk classification, Home Assistant configuration validation, pre-deployment backup, apply, reload/restart, observation, promotion, rejection marking and rollback remain downstream.

The next increment should compare the verified candidate stage with the relevant known-good configuration baseline and derive deterministic changed-file evidence suitable for dependency analysis and risk classification. Only after those checks and Home Assistant validation succeed may the transaction create a recoverable pre-deployment backup. Apply must remain after that backup and must be followed by runtime observation and automatic rollback on failure.
