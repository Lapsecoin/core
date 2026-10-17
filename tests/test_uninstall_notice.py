import os

import datadir
import uninstall_notice


def _env(tmp_path):
    return {"LOCALAPPDATA": str(tmp_path)}


def _wallet(tmp_path):
    folder = tmp_path / datadir.APP_DIR_NAME
    folder.mkdir()
    (folder / "lapsecoin_wallet.json").write_text("{}")
    return str(folder)


class TestRun:
    def test_shows_the_notice_when_a_wallet_is_on_disk(self, tmp_path):
        folder = _wallet(tmp_path)
        shown = []
        assert uninstall_notice.run(_env(tmp_path), show=shown.append) is True
        assert shown == [folder]

    def test_says_nothing_when_there_is_no_wallet(self, tmp_path):
        shown = []
        assert uninstall_notice.run(_env(tmp_path), show=shown.append) is False
        assert shown == []

    def test_a_window_that_cannot_open_never_blocks_the_uninstall(self, tmp_path):
        _wallet(tmp_path)

        def broken(folder):
            raise RuntimeError("no display")

        assert uninstall_notice.run(_env(tmp_path), show=broken) is False


def test_text_names_the_folder_and_the_phrase():
    msg = uninstall_notice.text(os.path.join("C:\\", "x"))
    assert "recovery phrase" in msg and "C:" in msg
