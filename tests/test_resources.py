"""Where a build finds its shipped files and its liboqs. A wrong answer here
means a packaged app that cannot start, which a source checkout never shows,
so each kind of build is imitated."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import resources  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def plain_python(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delitem(resources.__dict__, "__compiled__", raising=False)


def test_a_source_checkout_is_not_a_build_and_its_files_are_at_the_repository_root():
    assert resources.is_frozen() is False
    assert resources.bundle_dir() == ROOT
    assert os.path.isfile(os.path.join(resources.bundle_dir(), "VERSION"))


def test_a_pyinstaller_bundle_is_its_temporary_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.is_frozen() is True
    assert resources.bundle_dir() == str(tmp_path)


def test_a_nuitka_build_is_found_by_its_marker_and_keeps_files_beside_the_modules(monkeypatch):
    monkeypatch.setitem(resources.__dict__, "__compiled__", object())
    assert resources.is_frozen() is True
    assert resources.bundle_dir() == os.path.dirname(os.path.abspath(resources.__file__))


def test_liboqs_shipped_with_a_build_is_pointed_at_before_it_is_imported(monkeypatch, tmp_path):
    (tmp_path / "_oqs" / "bin").mkdir(parents=True)
    monkeypatch.setattr(resources, "bundle_dir", lambda: str(tmp_path))
    monkeypatch.setenv("OQS_INSTALL_PATH", "x")   # so the value is restored afterwards
    monkeypatch.delenv("OQS_INSTALL_PATH")
    assert resources.prepare_oqs() is True
    assert os.environ["OQS_INSTALL_PATH"] == str(tmp_path / "_oqs")


def test_without_a_shipped_liboqs_the_environment_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(resources, "bundle_dir", lambda: str(tmp_path))
    monkeypatch.setenv("OQS_INSTALL_PATH", "/somewhere/the/user/chose")
    assert resources.prepare_oqs() is False
    assert os.environ["OQS_INSTALL_PATH"] == "/somewhere/the/user/chose"
