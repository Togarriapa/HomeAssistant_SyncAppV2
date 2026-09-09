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

This increment does not claim to discover arbitrary custom Recorder database or
log destinations. Automatic periodic synchronization remains gated until those
locations can be explicitly discovered or configured and routed to their proper
dedicated branches. It also does not implement database/log publication or any
remote `candidate` deployment behavior.
