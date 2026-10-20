import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)
TEST = os.path.join(HERE, "..", "tools", "crash-relay", "worker.test.mjs")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_crash_relay_files_one_issue_per_crash_and_keeps_text_safe():
    done = subprocess.run(["node", "--test", TEST], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]
