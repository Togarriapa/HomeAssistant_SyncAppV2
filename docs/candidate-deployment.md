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

## Immutable change-detection boundary

`detect_candidate_changes()` implements the README's **Change detection** evidence needed by later validation. It compares the exact candidate commit with a stable, identity-bound Repo B `main` head rather than using mutable live Home Assistant bytes as the known-good baseline.

The change-detection boundary:

- re-verifies Candidate Fetch and Candidate Stage evidence before consuming either;
- re-proves the configured private Repo B identity and obtains the exact trusted `main` head;
- fetches that exact `main` commit only into the already-isolated candidate Git metadata under a temporary app ref;
- uses the same credential-free HTTPS and temporary non-interactive authentication boundary as candidate Fetch;
- requires the fetched baseline SHA and Git object type to exactly match the trusted `main` observation;
- validates both Git trees as regular `100644`/`100755` blobs with safe paths and rejects symlinks, submodules, traversal, `.git`, malformed IDs, duplicates and prefix conflicts;
- requires candidate Git path, mode and object-ID metadata to exactly match the verified Stage manifest before deriving differences;
- emits deterministic UTF-8-byte-ordered evidence for added, deleted, modified, executable-mode-changed, and combined content/mode changes;
- binds that evidence to Repo B target, stable repository ID, exact baseline SHA and exact candidate SHA;
- re-verifies Stage and Fetch after evidence generation, then re-observes Repo B `main` and fails closed if the known-good branch moved during the operation;
- removes the temporary baseline ref on every completion or failure path.

This step performs no merge and grants no deployment authority. A moved, absent or unverifiable `main`, identity mismatch, transport failure, staged-byte change, or Git/Stage metadata disagreement causes change detection to fail closed.

## Integrity validation boundary

`validate_candidate_integrity()` implements the README's explicit **Integrity validation** gate after change detection and before dependency analysis. It consumes only immutable Fetch, Stage and Change evidence and does not inspect or modify the live Home Assistant tree.

The integrity gate:

- requires Repo B target, stable repository ID, `candidate` branch identity and exact candidate SHA to agree across Fetch, Stage and Change evidence;
- requires a valid exact baseline SHA and the Stage manifest SHA-256 that will be carried forward by later gates;
- re-verifies the complete staged manifest, staged file set, modes and SHA-256 digests before validation;
- re-proves the isolated candidate Git ref before validation;
- validates deterministic change ordering, unique safe paths, supported status vocabulary and the required before/after mode and object-ID shape for every status;
- rejects impossible status claims such as an `added` path with baseline evidence, a `modified` path with unchanged content, or a content-only status that also changes executable mode;
- requires every candidate-side changed path, Git mode and object ID to match the verified Stage manifest exactly, while unchanged candidate files remain covered by whole-stage verification;
- re-verifies Stage and Fetch again immediately before returning success so later dependency analysis receives evidence tied to the same candidate bytes;
- returns immutable success evidence bound to target, repository ID, baseline SHA, candidate SHA, Stage manifest SHA-256 and deterministic changed paths.

Failure is sanitized and fail-closed. Integrity success grants no permission to validate with Home Assistant, create a backup, apply candidate bytes, reload/restart Home Assistant, promote a branch or perform rollback.

## Not implemented yet

No candidate byte from Stage is copied into the live Home Assistant configuration. Dependency analysis, risk classification, Home Assistant configuration validation, pre-deployment backup, apply, reload/restart, observation, promotion, rejection marking and rollback remain downstream.

The next increment should consume successful integrity evidence and immutable change evidence for **Dependency analysis**. Risk classification follows dependency analysis. Only after those gates and Home Assistant validation succeed may the transaction create a recoverable pre-deployment backup. Apply must remain after that backup and must be followed by runtime observation and automatic rollback on failure.
