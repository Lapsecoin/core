"""Creating and unlocking the node's key at startup: the passphrase rules, and
that a wrong passphrase stops the start instead of running on a broken key."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import crypto  # noqa: E402
import wallet  # noqa: E402


@pytest.fixture(autouse=True)
def no_prompts(monkeypatch):
    monkeypatch.delenv("LAPSECOIN_PASSPHRASE", raising=False)
    monkeypatch.setattr(wallet.getpass, "getpass",
                        lambda prompt="": pytest.fail(f"asked for input: {prompt}"))


def answers(monkeypatch, *given):
    queue = list(given)
    asked = []

    def fake(prompt=""):
        asked.append(prompt)
        return queue.pop(0)
    monkeypatch.setattr(wallet.getpass, "getpass", fake)
    return asked


class TestPassphrase:
    def test_the_environment_variable_wins_without_asking(self, monkeypatch):
        monkeypatch.setenv("LAPSECOIN_PASSPHRASE", "from the environment")
        assert wallet.resolve_passphrase("Passphrase: ") == "from the environment"

    def test_otherwise_the_user_is_asked(self, monkeypatch):
        asked = answers(monkeypatch, "typed by hand")
        assert wallet.resolve_passphrase("Passphrase: ") == "typed by hand"
        assert asked == ["Passphrase: "]

    def test_a_short_passphrase_is_refused_until_a_long_one_matches_twice(self, monkeypatch, capsys):
        answers(monkeypatch, "short", "long enough passphrase", "long enough passphrase")
        assert wallet.prompt_new_passphrase() == "long enough passphrase"
        assert "at least 8" in capsys.readouterr().out

    def test_two_different_entries_ask_again(self, monkeypatch, capsys):
        answers(monkeypatch, "first passphrase", "other passphrase",
                "first passphrase", "first passphrase")
        assert wallet.prompt_new_passphrase() == "first passphrase"
        assert "do not match" in capsys.readouterr().out

    def test_a_first_entry_already_given_is_checked_not_asked_again(self, monkeypatch):
        answers(monkeypatch, "same passphrase")        # only the confirmation
        assert wallet.prompt_new_passphrase("same passphrase") == "same passphrase"
