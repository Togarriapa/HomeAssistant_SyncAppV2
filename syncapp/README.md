# Home Assistant SyncApp V2

Private SSH configuration synchronization with explicit repository setup.

Version 0.2.0 adds an admin panel with deploy key generation, testing, safe
refresh and repository initialization. Automatic sync stays blocked until the
initial five-branch push succeeds. Local snapshots preserve raw file contents;
durable jobs reconcile interrupted pushes and refuse conflicting remote changes.

Candidate deployment and generated database/runtime/log data remain in development.
This experimental version mounts configuration read-only. See the Documentation
tab for setup, supported limits and recovery instructions.
