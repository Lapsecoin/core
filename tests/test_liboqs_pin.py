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


def _falcon_sources():
    lines = [ln.split() for ln in _read("native", "falcon_seeded", "SOURCES").splitlines()
             if ln.strip() and not ln.startswith("#")]
    version = next(ln[1] for ln in lines if ln[0] == "liboqs")
    return version, {name: digest for digest, name in (ln for ln in lines if ln[0] != "liboqs")}


def test_the_vendored_falcon_sources_are_the_pinned_liboqs_ones():
    """native/falcon_seeded builds the same PQClean files liboqs ships, so the
    keys it derives are the ones the wallet page publishes. They must stay the
    files of the pinned liboqs tag."""
    import hashlib
    version, hashes = _falcon_sources()
    assert version == next(iter(set(_versions().values()))), (
        "native/falcon_seeded/SOURCES names another liboqs than the pin: copy the files "
        "from the new tag and update SOURCES")
    for name, digest in hashes.items():
        with open(os.path.join(ROOT, "native", "falcon_seeded", name), "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == digest, f"{name} was changed"
