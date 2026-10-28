"""A paired build allocates one version; its children must not increment it."""

import importlib.util
from pathlib import Path

import pytest

_file = Path(__file__).resolve().parents[2] / "packaging/release_version.py"
_spec = importlib.util.spec_from_file_location("jm_release_version", _file)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)


def version_file(tmp_path):
    path = tmp_path / "ui/__init__.py"
    path.parent.mkdir()
    path.write_bytes(b'APP_NAME = "JM Studio"\r\nAPP_VERSION = "1.4.1"\r\n')
    return path


def test_allocate_once_then_share_with_windows_and_ubuntu(tmp_path):
    path = version_file(tmp_path)
    version = release.resolve_version(path=path)
    assert version == "1.4.2"
    assert release.resolve_version(version, path) == version
    assert release.resolve_version(version, path) == version
    assert release.metadata(path)["version"] == "1.4.2"
    assert b'\r\n' in path.read_bytes()


def test_each_new_release_increments_patch(tmp_path):
    path = version_file(tmp_path)
    assert release.resolve_version(path=path) == "1.4.2"
    assert release.resolve_version(path=path) == "1.4.3"


def test_mismatched_child_version_is_rejected(tmp_path):
    path = version_file(tmp_path)
    with pytest.raises(ValueError, match="version mismatch"):
        release.resolve_version("1.4.2", path)
    assert release.metadata(path)["version"] == "1.4.1"


def test_concurrent_allocation_is_rejected_without_changing_source(tmp_path):
    path = version_file(tmp_path)
    lock = tmp_path / "build/release-version.lock"
    lock.parent.mkdir()
    lock.touch()
    with pytest.raises(RuntimeError, match="Another release"):
        release.resolve_version(path=path)
    assert release.metadata(path)["version"] == "1.4.1"
