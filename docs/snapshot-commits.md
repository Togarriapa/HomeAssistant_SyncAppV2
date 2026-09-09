# Integrity-bound local snapshot commits

This document describes the local commit boundary introduced by issues #36 and #37. Its sole product requirement source is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Workspace identity

A mutable Git workspace carries the deterministic snapshot ID of the verified staging snapshot from which it was materialized. Before Git stages any file, `verify_workspace_content()` recursively re-hashes all non-Git content and reconstructs that snapshot identity from relative path, byte size, permission mode and SHA-256 digest.

The reserved root `.git` directory is excluded from this content identity because it is machine-owned repository metadata. Any nested `.git` path is rejected. Symlinks, hard-linked files, special files, additions, removals and byte or mode changes fail closed because they no longer reproduce the accepted snapshot identity.

## Commit contract

`create_snapshot_commit()` first validates the confined local repository and verifies workspace content. It stages the workspace through the fixed local Git runner. If Git reports no staged or working difference, the operation returns an explicit no-change result and does not create a commit.

When a meaningful difference exists, SyncApp creates a machine commit whose message references the verified snapshot ID. Git hooks and commit signing are forcibly disabled for machine operations so repository metadata cannot execute local hooks or trigger signing prompts. After committing, workspace content is re-verified before the resulting commit SHA is accepted and returned.

## Boundary

This increment creates only local Git commits inside the isolated mutable workspace. It does not configure a remote, authenticate, fetch, pull, push, publish Repo B, read or write live Home Assistant configuration, or deploy a remote candidate. Remote deployment remains subject to the validation, backup, deployment, observation and rollback requirements of the initial V2 README.
