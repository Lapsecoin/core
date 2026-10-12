"""The generated-icon cache is bounded and drops the least recently used."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import api


def _make(directory, name, size=10, age=0):
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    os.utime(path, (1_000_000 - age, 1_000_000 - age))
    return path


def test_under_the_caps_nothing_is_removed(tmp_path):
    for i in range(5):
        _make(tmp_path, f"lapsecoin-{i + 1}x{i + 1}.png")
    api._prune_icons(str(tmp_path))
    assert len(os.listdir(tmp_path)) == 5


def test_over_the_file_cap_the_least_recently_served_go_first(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_ICON_CACHE_MAX_FILES", 3)
    for i in range(6):
        _make(tmp_path, f"lapsecoin-{i + 1}x{i + 1}.png", age=100 - i)  # 1 oldest
    api._prune_icons(str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == [f"lapsecoin-{n}x{n}.png" for n in (4, 5, 6)]


def test_over_the_byte_cap_drops_oldest_until_it_fits(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_ICON_CACHE_MAX_BYTES", 25)
    _make(tmp_path, "lapsecoin-1x1.png", size=10, age=30)
    _make(tmp_path, "lapsecoin-2x2.png", size=10, age=20)
    _make(tmp_path, "lapsecoin-3x3.png", size=10, age=10)
    api._prune_icons(str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == ["lapsecoin-2x2.png", "lapsecoin-3x3.png"]


def test_the_sizes_the_app_links_to_and_other_files_are_never_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_ICON_CACHE_MAX_FILES", 0)
    monkeypatch.setattr(api, "_ICON_CACHE_MAX_BYTES", 0)
    _make(tmp_path, "lapsecoin-200x200.png", age=500)
    _make(tmp_path, "lapsecoin-512x512.png", age=500)
    _make(tmp_path, "lapsecoin.svg", age=500)
    _make(tmp_path, "notes.txt", age=500)
    _make(tmp_path, "lapsecoin-9x9.png")
    api._prune_icons(str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == [
        "lapsecoin-200x200.png", "lapsecoin-512x512.png", "lapsecoin.svg", "notes.txt"]
