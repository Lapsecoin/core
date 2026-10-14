"""Things in .github/workflows/release.yml that were wrong once and are cheap
to pin: the notes say how many commits they list, and the order in which
things are published."""
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

PATH = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "release.yml")


@pytest.fixture(scope="module")
def text():
    with open(PATH, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def jobs(text):
    return yaml.safe_load(text)["jobs"]


def test_the_notes_list_as_many_commits_as_their_heading_says(text):
    heading = int(re.search(r"### (\d+) largest commits", text).group(1))
    taken = int(re.search(r"sort -rn \| head -(\d+) \|", text).group(1))
    assert heading == taken == 5


def test_pypi_goes_last_because_a_published_version_cannot_be_taken_back(jobs):
    assert set(jobs["publish-pypi"]["needs"]) == {"version", "release"}
    assert {"test", "build-linux", "build-windows"} <= set(jobs["release"]["needs"])


def test_a_failed_publish_does_not_roll_back_a_release_that_succeeded(jobs):
    assert "publish-pypi" not in jobs["revert-on-failure"]["needs"]
    assert "release" in jobs["revert-on-failure"]["needs"]


def test_the_virustotal_scan_cannot_fail_or_hang_the_release(jobs):
    step = next(s for s in jobs["release"]["steps"] if "VirusTotal" in s.get("name", ""))
    assert step.get("continue-on-error") is True
    assert step.get("timeout-minutes", 0) >= 16        # longer than the script's own 15
    assert "VT_API_KEY" in step["env"]


def test_the_virustotal_badge_sits_in_the_heading_after_the_version(text):
    assert 'echo "## ${{ needs.version.outputs.tag }}  $BADGE"' in text
    assert "scripts/virustotal.py artifacts/windows/lapsecoin.msi /tmp/vt-badge.md" in text
