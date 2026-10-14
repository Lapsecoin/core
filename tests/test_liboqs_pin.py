"""The prebuilt liboqs is only found if four places name the same version, and
only matches the app if that is the liboqs that liboqs-python expects. Both
are checked here, so a bump in one place cannot leave the others behind (the
install scripts would then quietly fall back to a long build)."""

import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _versions():
    return {
        "release.yml LIBOQS_REF": re.search(r'LIBOQS_REF:\s*"([\d.]+)"', _read(".github", "workflows", "release.yml")).group(1),
        "liboqs.yml LIBOQS_VERSION": re.search(r'LIBOQS_VERSION:\s*"([\d.]+)"', _read(".github", "workflows", "liboqs.yml")).group(1),
        "install.sh LIBOQS_VERSION": re.search(r'^LIBOQS_VERSION="([\d.]+)"', _read("scripts", "install.sh"), re.M).group(1),
        "install.ps1 LiboqsVersion": re.search(r'\$LiboqsVersion\s*=\s*"([\d.]+)"', _read("scripts", "install.ps1")).group(1),
    }


def test_all_four_places_name_the_same_liboqs():
    found = _versions()
    assert len(set(found.values())) == 1, found


def test_the_pinned_liboqs_is_the_one_liboqs_python_wants():
    metadata = pytest.importorskip("importlib.metadata")
    try:
        installed = metadata.version("liboqs-python")
    except metadata.PackageNotFoundError:
        pytest.skip("liboqs-python is not installed")
    wanted = ".".join(installed.split(".")[:3])
    pinned = next(iter(set(_versions().values())))
    assert pinned == wanted, (
        f"liboqs-python {installed} builds liboqs {wanted} but the prebuilt is {pinned}; "
        "bump the version in release.yml, liboqs.yml, install.sh and install.ps1 together")


def test_the_install_scripts_look_for_the_release_the_workflow_publishes():
    assert "releases/download/liboqs-$LIBOQS_VERSION" in _read("scripts", "install.sh")
    assert "releases/download/liboqs-$LiboqsVersion" in _read("scripts", "install.ps1")
    assert "tag_name: liboqs-${{ env.LIBOQS_VERSION }}" in _read(".github", "workflows", "liboqs.yml")
    assert "liboqs-$LIBOQS_VERSION-linux-x86_64.tar.gz" in _read("scripts", "install.sh")
    assert "liboqs-$LiboqsVersion-windows-x64.zip" in _read("scripts", "install.ps1")
