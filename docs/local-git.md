# Confined local Git operations

This document describes the local-only Git boundary introduced by issues #33 and #34. Its sole product requirement source is the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Purpose

The initial V2 specification requires Git operations to occur in staging rather than in the live Home Assistant configuration directory. The isolated workspace layer provides that staging location. This layer is the first code allowed to invoke Git and deliberately exposes only local repository initialization and inspection.

`initialize_repository()` accepts the `GitWorkspace` produced from a verified snapshot, validates the expected workspace layout, and initializes `.git` inside the mutable workspace tree. It uses an explicit branch and repository-local machine identity. It never changes global Git configuration.

## Process confinement

Every Git subprocess receives the mutable workspace tree as its explicit working directory. Callers cannot supply another `cwd`. Git runs non-interactively with system/global configuration disabled and with a minimal environment rather than inheriting credentials or unrelated Git settings from the service process.

The public API does not expose arbitrary Git command execution. This increment does not support remotes, clone, fetch, pull, push, SSH, credentials or other network-capable operations. Command failures return a fixed error rather than incorporating captured stdout/stderr into exception text.

## Boundary

This layer does not publish Repo B, create commits, perform branch routing, read or write the live Home Assistant configuration, or process the remote `candidate` branch. Network transport and commit creation require separate reviewed slices. Remote deployment remains governed by the validation, backup, deployment, observation and rollback process in the initial V2 README.
