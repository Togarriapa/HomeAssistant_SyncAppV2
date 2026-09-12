# Routine candidate detection

The sole product specification for this capability is the initial V2 root
`README.md` at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README requires a new Repo B `candidate` commit to be detected before the
controlled deployment pipeline can proceed, while the separate Retrigger Work
Cron Job remains recovery for unprocessed candidate commits. Candidate detection
therefore has a normal producer that is independent of Retrigger recovery.

`CandidateDetectionService` is a bounded owner-loop primitive. It uses an
explicit monotonic interval and performs no remote observation when merely
started. When due, one tick reuses the existing repository-ID-bound candidate
detector to re-prove the configured private Repo B, observe only the exact
`candidate` branch, and durably enqueue only its immutable head SHA. A delayed
owner loop advances the next deadline from the current observation, so missed
intervals coalesce into one observation rather than producing a catch-up storm.

An absent candidate branch is a successful no-work result. Existing durable work
identity keeps repeated observation of the same SHA idempotent; an identical
candidate that has already succeeded or been deterministically blocked is not
rearmed by normal detection. A different trusted SHA has its own durable work
identity.

Invalid lifecycle state, invalid or backward monotonic time, missing service
configuration, repository verification failure, and candidate-detection failure
fail closed with sanitized service errors. The GitHub credential is retained only
as an in-memory service argument and is not part of the durable candidate work
identity.

This capability is **intake only**. It does not Fetch or Stage candidate bytes,
run integrity/dependency/risk/Home Assistant validation, create a backup, Apply
configuration, reload or restart Home Assistant, observe a deployment, promote a
candidate, tag a known-good release, or roll back. Those remain separate gates in
the initial README's controlled candidate transaction.

The existing Retrigger candidate detector remains unchanged and independent. It
continues to identify missed or unprocessed candidate commits if the normal
producer is interrupted or unavailable.

This increment intentionally introduces the bounded primitive before wiring it
into `__main__.py`. Owner-loop activation and shutdown integration are a separate
increment and require the primitive's exact pull-request head to pass CI first.
