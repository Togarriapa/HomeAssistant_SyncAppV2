import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from ha_syncapp import source_boundary
from ha_syncapp.source_boundary import SourceBoundaryError, ValidatedSource


def _report_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        source_boundary.os,
        "fstatvfs",
        lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY),
    )


def test_valid_source_returns_canonical_immutable_identity_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    marker = source / "configuration.yaml"
    marker.write_text("homeassistant:\n")
    before = {entry.name: entry.stat().st_mtime_ns for entry in source.iterdir()}
    _report_read_only(monkeypatch)

    validated = source_boundary.validate_read_only_source(source)

    assert validated.path == source.resolve()
    assert validated.device == source.stat().st_dev
    assert validated.inode == source.stat().st_ino
    assert {entry.name: entry.stat().st_mtime_ns for entry in source.iterdir()} == before
    assert marker.read_text() == "homeassistant:\n"


def test_writable_filesystem_evidence_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    monkeypatch.setattr(
        source_boundary.os,
        "fstatvfs",
        lambda fd: SimpleNamespace(f_flag=0),
    )

    with pytest.raises(SourceBoundaryError, match="not read-only"):
        source_boundary.validate_read_only_source(source)


def test_symlinked_source_is_rejected_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "homeassistant"
    alias.symlink_to(real, target_is_directory=True)
    _report_read_only(monkeypatch)

    with pytest.raises(SourceBoundaryError, match="safe directory"):
        source_boundary.validate_read_only_source(alias)


def test_regular_file_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "configuration.yaml"
    source.write_text("homeassistant:\n")

    with pytest.raises(SourceBoundaryError, match="safe directory"):
        source_boundary.validate_read_only_source(source)


def test_revalidation_rejects_stale_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    _report_read_only(monkeypatch)
    current = source_boundary.validate_read_only_source(source)
    stale = ValidatedSource(current.path, current.device, current.inode + 1)

    with pytest.raises(SourceBoundaryError, match="no longer matches"):
        source_boundary.revalidate_source(stale)


def test_revalidation_accepts_unchanged_read_only_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "homeassistant"
    source.mkdir()
    _report_read_only(monkeypatch)
    validated = source_boundary.validate_read_only_source(source)

    source_boundary.revalidate_source(validated)
