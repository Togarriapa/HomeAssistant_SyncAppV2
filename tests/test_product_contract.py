from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_primary_docs_define_complete_tree_visibility() -> None:
    readme = _text("README.md")
    architecture = _text("docs/architecture.md")
    roadmap = _text("docs/roadmap.md")

    for document in (readme, architecture, roadmap):
        lowered = document.lower()
        assert "complete" in lowered
        assert "home assistant tree" in lowered
        assert "logs" in lowered

    assert "logs are the sole intentional exception" in readme.lower()
    assert ".storage" in readme
    assert "secrets" in readme.lower()
    assert "database" in readme.lower()
    assert "generated/runtime" in readme.lower()


def test_primary_docs_preserve_guarded_remote_update_transaction() -> None:
    required = "Detect → Fetch → Stage → Validate → Backup → Apply → Verify → Rollback if necessary"
    readme = _text("README.md")
    roadmap = _text("docs/roadmap.md")
    development = _text("docs/development.md")

    assert required in readme
    assert required in roadmap
    assert required in development
    assert "Never run `git pull`" in readme


def test_roadmap_rejects_blanket_file_class_exclusions() -> None:
    roadmap = _text("docs/roadmap.md").lower()
    assert "remove blanket exclusions" in roadmap
    assert "logs as the sole intentional exception" in roadmap
    assert "unknown/unsupported mutation classes fail closed" in roadmap
