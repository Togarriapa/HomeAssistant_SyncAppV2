# Candidate-bound pre-deployment backup

The sole product specification for this capability is the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires a recoverable Home Assistant backup after candidate validation and before any candidate bytes are applied. `candidate_backup.py` implements only that pre-deployment backup evidence boundary. It does not copy candidate files into the live configuration, reload or restart Home Assistant, observe a deployment, promote `candidate` to `main`, or perform rollback.

Before contacting Supervisor, the backup gate re-verifies the exact successful semantic-validation result against its static validation, integrity, Stage, dependency, impact, risk, runtime and Core-version evidence. A changed or mismatched candidate therefore cannot inherit a backup authorization produced for another candidate.

The gate uses the supported Supervisor backup API with `SUPERVISOR_TOKEN`. The app requests the least-privilege Supervisor `backup` role rather than manager/admin access. It creates a synchronous full backup and then queries the returned backup slug. Success requires the queried backup to have the exact returned slug, type `full`, and the same Home Assistant Core version bound into semantic validation. If Supervisor also returns the compact `content` model, Home Assistant content must be explicitly present.

Successful `CandidateBackupEvidence` binds the backup slug to repository ID, baseline SHA, candidate SHA, Stage manifest SHA-256, runtime SHA-256, risk level and exact Home Assistant version. The semantic evidence is re-verified after the backup lookup before this evidence is returned.

Supervisor responses are bounded and parsed as JSON with duplicate keys and non-finite constants rejected. Backup identifiers are constrained before they are used in a request path. Transport exceptions and response bodies are not propagated into diagnostic exceptions, preventing credentials or candidate content from being copied into logs accidentally.

Successful backup evidence can now be durably associated with an immutable deployment identity through the [prepared deployment state API](prepared-deployment-state.md). A later deployment-orchestration slice must independently re-prove every required gate before Apply; reading a persisted record alone does not authorize deployment. Observation, promotion, known-good tagging, fast rollback and full backup restore remain separate downstream safety milestones.


## Pre-Apply backup re-proof

`reprove_prepared_candidate_backup()` is a read-only boundary for a later Apply-authorization transaction. It accepts one structurally valid, immutable `PreparedDeployment` and re-verifies the complete semantic, static, integrity, Stage, dependency, impact, risk, runtime and Core-version chain. Every prepared candidate binding must match the freshly verified semantic evidence.

The boundary performs exactly one bounded `GET /backups/<persisted-slug>/info` request. It never creates a replacement backup. The response must identify the exact persisted slug, a full backup, and the exact Home Assistant Core version; when Supervisor supplies content metadata, Home Assistant content must be explicitly present. Semantic and Stage evidence are verified again after Supervisor I/O to reject concurrent drift.

Success is evidence only. It does not authorize or perform Apply, reload, restart, observation, promotion, tagging, rejection or rollback, and it grants no Git mutation authority. A later gate must independently re-prove current private Repo B identity and exact branch heads before any live mutation. Missing, changed or malformed evidence and Supervisor failures are sanitized and fail closed.
