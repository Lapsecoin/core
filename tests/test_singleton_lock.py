"""Two nodes on one data folder would write the same chain database, so the
second must be turned away, and a crashed first must never leave the lock
stuck. The lock is the operating system's, so these tests use real files and
a real second process."""
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import singleton_lock  # noqa: E402
from singleton_lock import SingleInstanceLock  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src")

pytestmark = pytest.mark.skipif(os.name == "nt", reason="flock is the POSIX half; CI covers Windows by running the app")


def test_the_first_instance_gets_the_lock_and_records_its_pid(tmp_path):
    lock = SingleInstanceLock(str(tmp_path / "node.lock"))
    assert lock.acquire() is True
    assert lock.holder_pid() == os.getpid()
    lock.release()


def test_a_second_instance_is_turned_away_and_can_say_who_holds_it(tmp_path):
    path = str(tmp_path / "node.lock")
    first, second = SingleInstanceLock(path), SingleInstanceLock(path)
    assert first.acquire() is True
    assert second.acquire() is False
    assert second.holder_pid() == os.getpid()
    first.release()


def test_the_lock_is_free_again_after_release(tmp_path):
    path = str(tmp_path / "node.lock")
    first = SingleInstanceLock(path)
    assert first.acquire()
    first.release()
    assert SingleInstanceLock(path).acquire() is True


def test_releasing_twice_or_without_acquiring_is_harmless(tmp_path):
    lock = SingleInstanceLock(str(tmp_path / "node.lock"))
    lock.release()
    assert lock.acquire()
    lock.release()
    lock.release()


def test_a_missing_folder_is_created(tmp_path):
    lock = SingleInstanceLock(str(tmp_path / "a" / "b" / "node.lock"))
    assert lock.acquire() is True
    lock.release()


def test_a_stale_pid_in_the_file_does_not_block_a_start(tmp_path):
    path = tmp_path / "node.lock"
    path.write_text("999999\n")          # left by a process that crashed
    lock = SingleInstanceLock(str(path))
    assert lock.acquire() is True
    assert lock.holder_pid() == os.getpid()
    lock.release()


def test_an_unreadable_pid_is_none_not_an_error(tmp_path):
    path = tmp_path / "node.lock"
    path.write_text("not a number")
    assert SingleInstanceLock(str(path)).holder_pid() is None
    assert SingleInstanceLock(str(tmp_path / "absent.lock")).holder_pid() is None


def test_an_unexpected_failure_checking_the_lock_lets_the_node_start(tmp_path, monkeypatch):
    def broken(*a):
        raise RuntimeError("no such syscall")
    monkeypatch.setattr(singleton_lock.fcntl, "flock", broken)
    assert SingleInstanceLock(str(tmp_path / "node.lock")).acquire() is True


def test_a_killed_process_frees_the_lock_for_the_next_start(tmp_path):
    path = str(tmp_path / "node.lock")
    code = (f"import sys, time; sys.path.insert(0, {SRC!r});"
            "from singleton_lock import SingleInstanceLock;"
            f"l = SingleInstanceLock({path!r}); print(l.acquire(), flush=True); time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "True"
        mine = SingleInstanceLock(path)
        assert mine.acquire() is False, "a live node in another process holds it"
        assert mine.holder_pid() == child.pid
        child.kill()                      # no clean shutdown, no cleanup code runs
        child.wait(10)
        deadline = time.time() + 5
        while not mine.acquire():
            assert time.time() < deadline, "the lock stayed stuck after the holder died"
            time.sleep(0.05)
        mine.release()
    finally:
        if child.poll() is None:
            child.kill()
