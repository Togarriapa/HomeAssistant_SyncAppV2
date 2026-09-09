import os
from pathlib import Path

import pytest
from ha_syncapp.errors import Failure
from ha_syncapp.files import Snapshot, capture, valid_path


def test_snapshot_preserves_binary_crlf_and_hidden_storage(tmp_path: Path) -> None:
    source = tmp_path / "ha"
    source.mkdir()
    (source / ".storage").mkdir()
    expected = {
        "configuration.yaml": b"homeassistant:\r\n",
        "secrets.yaml": b"token: fake\n",
        ".storage/registry": b'{"data":{}}\n',
        "www/image.bin": bytes(range(256)),
    }
    for name, data in expected.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (source / "home-assistant_v2.db").write_bytes(b"live-db")
    (source / "home-assistant_v2.db-wal").write_bytes(b"live-wal")
    (source / "home-assistant.log.1").write_bytes(b"logs")
    snap = capture(source)
    assert snap.files == expected
    target = tmp_path / "staged"
    snap.save(target)
    assert Snapshot.load(target).files == expected
    assert capture(source).digest == snap.digest
    (target / "files/secrets.yaml").write_bytes(b"tampered")
    with pytest.raises(Failure, match="snapshot_corrupt"):
        Snapshot.load(target)


@pytest.mark.parametrize(
    "path", ["../x", "/x", "a/../x", ".git/config", "a//b", "a\\b", "x\ny", "x\0y", "a/"]
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    with pytest.raises(Failure):
        valid_path(path)


def test_links_and_special_files_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_bytes(b"{}")
    (tmp_path / "evil").symlink_to("/etc/passwd")
    with pytest.raises(Failure):
        capture(tmp_path)
    (tmp_path / "evil").unlink()
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(Failure):
        capture(tmp_path)


def test_snapshot_limits_and_required_configuration(tmp_path: Path) -> None:
    with pytest.raises(Failure):
        capture(tmp_path)
    (tmp_path / "configuration.yaml").write_bytes(b"12345")
    with pytest.raises(Failure):
        capture(tmp_path, max_bytes=4)
