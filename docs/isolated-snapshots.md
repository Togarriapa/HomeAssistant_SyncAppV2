# Isolated source snapshots

This document describes the source-snapshot primitive implemented for issues #20 and #21. Its sole product requirement source is the initial `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

## Purpose

The initial V2 specification requires local Home Assistant changes to be synchronized through a consistent staging snapshot, with copied-file integrity verification, and requires Git operations to occur in staging rather than inside the live Home Assistant configuration directory.

`ha_syncapp.snapshot` implements only that boundary. It does **not** perform Git operations, choose Repo B branches, mount the Home Assistant configuration, publish data, or deploy remote candidates.

## Capture contract

`capture_snapshot(source, staging_root)` requires two existing, real, non-overlapping directories. It walks the source without intentionally following symbolic links and copies regular files into a newly created staging directory. Files are copied byte-for-byte and represented in a deterministic manifest containing relative path, byte size, permission-mode bits and SHA-256 digest.

The initial primitive fails closed on filesystem objects whose semantics cannot be represented safely by this staging model: symbolic links, hard-linked files and special files. This is deliberate. A later requirement may add an explicit representation for such objects, but silently dereferencing or dropping them would violate the reliability and byte-fidelity goals of the root specification.

Source metadata is captured before copying and rescanned before acceptance. File descriptors are checked before and after reads. If the source changes, gains or loses a file, or otherwise becomes inconsistent during capture, the snapshot is rejected and incomplete staging is removed.

## Verification contract

`verify_snapshot(snapshot_root)` re-enumerates the staged tree, rejects unsafe filesystem objects, re-hashes every file and validates the canonical manifest and snapshot identity. Later Git publication work must call this verification boundary immediately before consuming staged content rather than assuming that a previously accepted staging directory is still trustworthy.

## Explicit non-goals

This increment does not:

- run `git` against either staging or the live Home Assistant configuration;
- implement local-to-Repo-B branch routing;
- handle Recorder database consistency or log retention;
- detect or deploy the `candidate` branch;
- validate Home Assistant configuration;
- create backups;
- modify the running Home Assistant installation;
- implement observation or rollback.

Remote changes therefore remain outside the scope of this primitive and must eventually use the controlled candidate validation, backup, deployment, observation and rollback process defined by the initial V2 README.
