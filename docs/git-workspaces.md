# Isolated Git workspaces

This document describes the workspace boundary introduced by issues #30 and #31. Its sole product requirement source is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Purpose

The initial V2 specification requires Local → GitHub synchronization to operate from a consistent, integrity-verified staging snapshot and explicitly requires Git operations to occur in staging rather than in the live Home Assistant configuration directory.

The accepted snapshot is evidence and is kept immutable. `prepare_git_workspace()` therefore creates a second, unique staging tree that is intentionally mutable and can later become a Git working tree. This prevents future Git commands from changing the snapshot whose manifest justified publication.

## Materialization contract

Before copying, the complete snapshot is re-verified against its canonical manifest. The workspace root must be a real, existing directory and must not overlap the accepted snapshot root. Each call creates a fresh unique workspace rather than reusing caller-controlled Git state.

Every accepted file is copied byte-for-byte and checked against the manifest-bound size, permission mode and SHA-256 digest. File descriptors are checked before and after copying. The accepted snapshot is fully re-verified again after materialization, so tampering while copying fails closed. Partial workspaces are removed on failure.

A top-level `.git` path in snapshot content is rejected because it would collide with the Git metadata namespace of the future mutable workspace. This is an explicit fail-closed representation boundary, not a silent exclusion.

## Separation of responsibilities

This increment does not initialize a Git repository, create Git metadata, configure remotes, authenticate to GitHub, fetch, commit, push, mount Home Assistant configuration, or write to a running Home Assistant installation. Those operations remain later slices and must consume this isolated workspace boundary.

Remote candidate deployment remains entirely separate and continues to require controlled validation, backup, deployment, observation and rollback as defined by the initial V2 README.
