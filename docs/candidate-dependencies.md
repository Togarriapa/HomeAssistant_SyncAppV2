# Candidate dependency evidence

This increment is derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, especially the **Remote → Home Assistant Deployment**, **runtime**, and **Dependency and Topology Model** sections.

`ha_syncapp.candidate_dependencies.analyze_candidate_dependencies()` is a read-only gate after Candidate Integrity Validation. It accepts the immutable integrity evidence, the exact isolated Stage evidence, and already-collected Home Assistant runtime inventory. It never executes candidate code or templates and performs no Home Assistant writes, service calls, reloads or restarts.

## Binding and reverification

The analysis requires the target repository identity, candidate SHA and Stage manifest digest in Candidate Integrity evidence to match the Stage evidence exactly. Stage is reverified before and after analysis. Each analyzed candidate-side changed file is read from the isolated Stage using the Stage file-safety primitive and its bytes are rebound to the manifest size and SHA-256 before parsing.

The runtime fingerprint binds entity and service inventory to the result. Verification recomputes that fingerprint and also revalidates entity/service evidence structure so malformed runtime evidence cannot be accepted merely because a previous result object exists.

Paths present in Candidate Integrity but absent from the candidate Stage are represented as deletions. Unchanged Stage files are not analyzed by this candidate-change gate.

## Conservative static coverage

The current analyzer intentionally provides conservative lexical evidence rather than claiming to interpret the complete Home Assistant configuration language. Literal `domain.object` references found in bounded UTF-8 changed text are classified against exact identifiers in the collected runtime evidence.

Three result classes are exposed:

- `known_entity_references`: exact entity IDs present in runtime entity inventory.
- `known_service_references`: exact `domain.service` identifiers proven by a runtime service inventory entry that contains explicit service names.
- `unknown_object_references`: lexical object-shaped references not proven by either inventory.

Runtime service summaries that contain only a domain remain valid input for compatibility with existing runtime collection. They do **not** prove any particular service name, so a candidate reference such as `homeassistant.restart` remains unresolved unless the exact `restart` service is present in that domain's `services` mapping.

Service domains and names must be lowercase Home Assistant-style identifier components (`[a-z0-9_]+`). Duplicate domains, malformed service structures, or malformed service identities fail closed. This prevents malformed runtime evidence from silently suppressing an unknown-object warning.

Because the current parser is lexical rather than syntax-aware, an identifier that is simultaneously asserted by runtime evidence as both an entity and a service is treated as ambiguous and fails validation instead of guessing its meaning.

Jinja/template and include/secret indirection markers are surfaced through `dynamic_reference`/`dynamic_paths`. The analyzer never evaluates those expressions. Binary/non-UTF-8 and oversize files are retained as explicit `unanalyzed_paths`; they are never treated as dependency-free merely because static analysis could not inspect them.

This means downstream Risk Classification must treat dynamic, unknown, ambiguous or unanalyzed coverage conservatively. Dependency evidence cannot authorize deployment by itself.

## Security boundary

Candidate contents are not copied into exception messages or logs. Analysis occurs only against isolated Stage bytes and returns reference identifiers needed for dependency reasoning. The live Home Assistant configuration is not used as a Git working tree and is not modified by this gate.

Future increments under #133 can add richer syntax-aware automation/script/scene relationships, but they must preserve these integrity bindings, fail-closed coverage reporting and non-execution guarantees.
