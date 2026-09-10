# Candidate-bound pre-deployment backup

The sole product specification for this capability is the initial V2 root `README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires a recoverable Home Assistant backup after candidate validation and before any candidate bytes are applied. `candidate_backup.py` implements only that pre-deployment backup evidence boundary. It does not copy candidate files into the live configuration, reload or restart Home Assistant, observe a deployment, promote `candidate` to `main`, or perform rollback.

Before contacting Supervisor, the backup gate re-verifies the exact successful semantic-validation result against its static validation, integrity, Stage, dependency, impact, risk, runtime and Core-version evidence. A changed or mismatched candidate therefore cannot inherit a backup authorization produced for another candidate.

The gate uses the supported Supervisor backup API with `SUPERVISOR_TOKEN`. The app requests the least-privilege Supervisor `backup` role rather than manager/admin access. It creates a synchronous full backup and then queries the returned backup slug. Success requires the queried backup to have the exact returned slug, type `full`, and the same Home Assistant Core version bound into semantic validation. If Supervisor also returns the compact `content` model, Home Assistant content must be explicitly present.

Successful `CandidateBackupEvidence` binds the backup slug to repository ID, baseline SHA, candidate SHA, Stage manifest SHA-256, runtime SHA-256, risk level and exact Home Assistant version. The semantic evidence is re-verified after the backup lookup before this evidence is returned.

Supervisor responses are bounded and parsed as JSON with duplicate keys and non-finite constants rejected. Backup identifiers are constrained before they are used in a request path. Transport exceptions and response bodies are not propagated into diagnostic exceptions, preventing credentials or candidate content from being copied into logs accidentally.

This increment intentionally stops at backup evidence. A later deployment-orchestration slice must durably associate this evidence with its deployment record and must independently re-prove every required gate before Apply. Observation, promotion, known-good tagging, fast rollback and full backup restore remain separate downstream safety milestones.