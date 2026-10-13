import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lapsecoin_bridge as bridge  # noqa: E402


def test_an_installed_app_is_just_started(monkeypatch, tmp_path):
    exe = tmp_path / "Local" / "Programs" / "LapseCoin" / "lapsecoin.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    started = []
    monkeypatch.setattr(bridge, "start", lambda e, a: started.append((e, a)))
    monkeypatch.setattr(bridge, "install", lambda v: (_ for _ in ()).throw(AssertionError))
    assert bridge.main(["--no-gui"]) == 0
    assert started == [(str(exe), ["--no-gui"])]


def test_first_run_copies_data_installs_then_starts(monkeypatch, tmp_path):
    local = tmp_path / "Local"
    old = tmp_path / "old"
    old.mkdir()
    (old / "lapsecoin_key.json").write_text("key")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(sys, "executable", str(old / "lapsecoin.exe"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge, "say", lambda t: None)
    monkeypatch.setattr(bridge, "local_version", lambda: "9.9.9")
    started = []
    monkeypatch.setattr(bridge, "start", lambda e, a: started.append(e))

    def fake_install(version):
        assert version == "9.9.9"
        exe = local / "Programs" / "LapseCoin" / "lapsecoin.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")

    monkeypatch.setattr(bridge, "install", fake_install)
    assert bridge.main([]) == 0
    assert (local / "LapseCoin" / "lapsecoin_key.json").read_text() == "key"
    assert (old / "lapsecoin_key.json").exists()
    assert len(started) == 1


def test_a_failed_install_keeps_the_data_and_says_where_to_download(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "lapsecoin.exe"))
    monkeypatch.chdir(tmp_path)
    said = []
    monkeypatch.setattr(bridge, "say", said.append)
    monkeypatch.setattr(bridge, "local_version", lambda: "9.9.9")
    monkeypatch.setattr(bridge, "install", lambda v: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(bridge, "start", lambda e, a: (_ for _ in ()).throw(AssertionError))
    assert bridge.main([]) == 1
    assert any("releases/latest" in t for t in said)
