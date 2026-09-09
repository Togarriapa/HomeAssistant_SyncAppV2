# Home Assistant source boundary

The Home Assistant App manifest requests the supported `homeassistant_config`
mount at `/homeassistant` as read-only. Before Local → Repo B synchronization can
consume that path, V2 also validates the runtime mount rather than trusting the
manifest declaration alone.

Validation opens the source directory with `O_DIRECTORY` and `O_NOFOLLOW`,
records its device/inode identity, checks that the mounted filesystem reports
`ST_RDONLY`, resolves the canonical path, and verifies that the canonical path
and the original open descriptor still identify the same directory. It creates
no probe file and performs no write in the Home Assistant tree.

The resulting immutable source evidence can be revalidated before a later
operation. A writable mount, symlink, missing path, type mismatch, or identity
change fails closed.

This is an additional safety gate, not a substitute for real Home Assistant OS
validation. Before enabling automatic Local → Repo B synchronization on hardware,
install V2 on a test Home Assistant OS system and confirm that Supervisor exposes
`/homeassistant` as the expected read-only `homeassistant_config` mount.

Candidate deployment is deliberately unaffected. Applying a remote candidate
will require a separately justified write boundary while still preserving the
initial V2 README's validation, backup, apply, reload/restart, observation and
rollback sequence.
