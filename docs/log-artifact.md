# Log artifact staging boundary

This document describes one incremental capability derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

The README defines Repo B `logs` as an outbound Home Assistant -> GitHub branch with four diagnostic categories: `home-assistant`, `supervisor`, `syncapp`, and `deployments`. It requires a 30-day retention period and explicitly notes that removing files from the current Git tree does not remove historical commits.

## What this increment does

`ha_syncapp.log_artifact` accepts explicit already-collected `LogRecord` values and stages a deterministic current-retention snapshot under:

```text
logs/
├── home-assistant/records.jsonl
├── supervisor/records.jsonl
├── syncapp/records.jsonl
└── deployments/records.jsonl
```

A canonical `manifest.json` records the explicit UTC reference time, 30-day retention policy, per-category retained record counts, and SHA-256/size evidence for each category file. The manifest SHA-256 is the artifact identifier.

Artifact generation is deterministic: record ordering is canonical, timezone-aware timestamps are normalized to UTC, the retention cutoff is calculated from a caller-supplied reference time rather than hidden wall-clock state, and all four README categories are represented even when empty.

The staging root, artifact directories, and files are private (`0700` directories and `0600` files). Verification rechecks the complete expected layout, directory/file modes, single-link regular-file semantics, bytes, SHA-256 evidence, manifest structure, and artifact identifier. Unexpected entries, symlinks, hard-linked files, mode changes, byte changes, missing paths, or evidence tampering fail closed.

Resource use is bounded. One artifact accepts at most 10,000 records, one record message may encode to at most 1 MiB, and the complete staged artifact may encode to at most 16 MiB. A record timestamp later than the explicit reference time is rejected rather than silently changing retention semantics.

## What this increment does not do

This module does **not** collect Home Assistant, Supervisor, SyncApp, or deployment logs. It does not call Home Assistant or Supervisor APIs, read arbitrary environment variables, publish Git, rewrite Git history, install a schedule, process Retrigger work, or modify the live Home Assistant installation.

The 30-day rule here governs only the **current staged snapshot**. The README separately warns that Git history can retain removed data. A later guarded logs-publication/history-maintenance milestone must define bounded branch-history pruning/rebuilding without pretending that Git history rewriting is forensic secure deletion.

The candidate deployment pipeline is unaffected. In particular, this capability does not authorize Home Assistant backup or candidate Apply while semantic candidate validation remains unresolved.
