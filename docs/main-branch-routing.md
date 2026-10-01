# Repo B main branch routing

The initial V2 README separates Home Assistant configuration from dedicated
Recorder database and diagnostic log history. Repo B `main` therefore uses an
explicit path-routing policy before snapshot evidence is created; this boundary
is not implemented with `.gitignore` and does not depend on Git silently dropping
files.

The current policy routes the standard root-level Home Assistant Recorder family
`home-assistant_v2.db`, including SQLite `-wal`, `-shm`, and other sidecars, away
from `main`. It also routes the standard root-level `home-assistant.log` family,
including rotations and fault variants, away from `main`.

The rule is deliberately narrow. A file such as
`custom_components/demo/data.db`, `packages/notes.log`, or a similarly named file
below another directory remains configuration data and is preserved in `main`.
Persistent configuration and state such as `.storage`, `secrets.yaml`, dashboards,
packages, blueprints and custom components remain byte-for-byte eligible for the
configuration snapshot.

Path selection happens during both the initial and final source scans. Changes to
a dedicated standard database/log artifact therefore do not make a `main`
snapshot unstable, while any selected configuration mutation continues to fail
closed.

An explicitly configured `recorder_database_path` is converted to one validated
path relative to the same Home Assistant source root. That exact database path
and its SQLite sidecar family are excluded consistently from repository
initialization, startup and routine Local snapshots, event observation, and
Retrigger processing. The router rejects relative, root, traversal, and
out-of-source Recorder paths; it does not broadly discard unrelated nested
`.db` files.

Recorder publication, Core/Supervisor log collection and publication, and remote
`candidate` deployment are implemented by separate guarded lanes. This routing
component grants none of their authority. Custom log destinations are not
discovered or guessed; the Local route continues to separate only the standard
root-level Home Assistant log family.
