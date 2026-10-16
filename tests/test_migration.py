"""An old single-key node moving to a recovery-phrase wallet: the sweep is
signed once, stored, and only then is the old key deleted; a restart at any
point finishes the job."""

import os
import types

import pytest

import crypto
import falcon_seeded
import migration
import state as state_mod
import tx as tx_mod
import wallet
from params import TICKS_PER_LAPSE
from walletfile import WalletFile
from tests.legacy_key import save_key

pytestmark = pytest.mark.skipif(
    not falcon_seeded.available(), reason="native/falcon_seeded is not built")

PASS = "migration test passphrase"
BALANCE = 50 * TICKS_PER_LAPSE


class FakeNode:
    def __init__(self, state, synced=True, peers=3):
        self.running = True
        self.view = types.SimpleNamespace(state=state)
        self.mempool = types.SimpleNamespace(pending_nonce=lambda addr: 0)
        self.synced, self.peers = synced, peers
        self.submitted = []
        self.accept = True

    def get_info(self):
        return {"sync_percent": 100 if self.synced else 40, "peer_count": self.peers}

    def submit_tx_from_api(self, t):
        self.submitted.append(t)
        return (True, "h") if self.accept else (False, "node busy")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in (wallet.KEY_ENV, wallet.PASSPHRASE_ENV):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def old_node(home):
    """An old single-key file, funded on a fake chain."""
    sk, pk = crypto.generate_keypair()
    save_key(wallet.LEGACY_KEY_FILE, sk, pk, PASS)
    st = state_mod.State()
    old_addr = crypto.public_key_to_address(pk)
    st.credit(old_addr, BALANCE)
    return types.SimpleNamespace(addr=old_addr, state=st, node=FakeNode(st))


def start(monkeypatch, capsys=None):
    monkeypatch.setenv(wallet.PASSPHRASE_ENV, PASS)
    w, kek, _ = wallet.startup_console()
    return w


def test_start_creates_the_wallet_and_keeps_the_old_key_until_the_sweep_is_stored(
        home, old_node, monkeypatch, capsys):
    w = start(monkeypatch)
    assert os.path.exists(wallet.WALLET_FILE) and os.path.exists(wallet.LEGACY_KEY_FILE)
    assert w.migration is not None and w.addr != old_node.addr
    assert "recovery phrase" in capsys.readouterr().out.lower()
    assert WalletFile.load(wallet.WALLET_FILE).phrase_shown


def test_the_sweep_moves_everything_but_the_fee_and_deletes_the_old_key(home, old_node, monkeypatch):
    w = start(monkeypatch)
    assert w.migration.step(old_node.node) == "signed"
    assert not os.path.exists(wallet.LEGACY_KEY_FILE)
    sweep = WalletFile.load(wallet.WALLET_FILE).pending_sweep
    assert sweep["from"] == old_node.addr
    assert sweep["outputs"] == [{"to": w.addr, "amount": BALANCE - sweep["fee"]}]
    assert sweep["nonce"] == 1 and sweep["fee"] > 0
    # it is a transaction the chain accepts as it stands
    assert tx_mod.validate(sweep, old_node.state) == (True, None)


def test_it_waits_for_the_node_to_sync_and_changes_nothing_meanwhile(home, old_node, monkeypatch):
    w = start(monkeypatch)
    old_node.node.synced = False
    assert w.migration.step(old_node.node) == "waiting"
    old_node.node.synced, old_node.node.peers = True, 0
    assert w.migration.step(old_node.node) == "waiting"
    assert os.path.exists(wallet.LEGACY_KEY_FILE)
    assert WalletFile.load(wallet.WALLET_FILE).pending_sweep is None


def test_stored_sweep_is_broadcast_then_cleared_once_the_nonce_moves(home, old_node, monkeypatch):
    w = start(monkeypatch)
    node = old_node.node
    w.migration.step(node)
    assert w.migration.step(node) == "sent" and len(node.submitted) == 1
    assert w.migration.step(node, now=10_000_000.0) == "sent" and len(node.submitted) == 2
    # included in a block: the old address's nonce is now 1
    sweep = WalletFile.load(wallet.WALLET_FILE).pending_sweep
    node.view.state = types.SimpleNamespace(
        get_nonce=lambda a: sweep["nonce"], get_balance=lambda a: 0)
    assert w.migration.step(node) == "confirmed"
    assert WalletFile.load(wallet.WALLET_FILE).pending_sweep is None
    assert w.migration.done


def test_a_restart_before_the_sweep_picks_the_old_key_up_again(home, old_node, monkeypatch):
    start(monkeypatch)                       # then the process died
    w2 = start(monkeypatch)                  # both files exist
    assert w2.migration is not None and w2.migration.legacy is not None
    assert w2.migration.step(old_node.node) == "signed"


def test_a_restart_after_the_sweep_is_stored_just_rebroadcasts_it(home, old_node, monkeypatch):
    w = start(monkeypatch)
    w.migration.step(old_node.node)
    w2 = start(monkeypatch)                  # old key file is gone by now
    assert w2.migration is not None and w2.migration.legacy is None
    node = old_node.node
    assert w2.migration.step(node) == "sent" and node.submitted


def test_a_balance_too_small_to_move_just_retires_the_key(home, monkeypatch):
    sk, pk = crypto.generate_keypair()
    save_key(wallet.LEGACY_KEY_FILE, sk, pk, PASS)
    st = state_mod.State()
    st.credit(crypto.public_key_to_address(pk), 3)
    w = start(monkeypatch)
    assert w.migration.step(FakeNode(st)) == "empty"
    assert not os.path.exists(wallet.LEGACY_KEY_FILE)
    assert w.migration.done


def test_the_nonce_continues_after_the_old_address_own_history(home, old_node, monkeypatch):
    old_node.state._nonces[old_node.addr] = 7
    w = start(monkeypatch)
    w.migration.step(old_node.node)
    assert WalletFile.load(wallet.WALLET_FILE).pending_sweep["nonce"] == 8


def test_a_fresh_wallet_has_no_migration(home, monkeypatch):
    assert start(monkeypatch).migration is None


def test_funds_that_arrive_after_the_move_are_reported(home, old_node, monkeypatch, caplog):
    w = start(monkeypatch)
    w.migration.step(old_node.node)
    sweep = WalletFile.load(wallet.WALLET_FILE).pending_sweep
    old_node.node.view.state = types.SimpleNamespace(
        get_nonce=lambda a: sweep["nonce"], get_balance=lambda a: 123)
    with caplog.at_level("WARNING"):
        w.migration.step(old_node.node)
    assert "cannot be reached" in caplog.text


# A restored phrase finds its other addresses
def test_restore_scan_lists_the_used_accounts_and_stops_after_a_gap(home, monkeypatch):
    import hdwallet
    phrase = "abandon " * 11 + "about"
    w, kek, _ = wallet.create_wallet(PASS, phrase=phrase)
    used = []
    for i in (1, 3):
        _sk, pk = falcon_seeded.keypair_from_seed(hdwallet.falcon_seed_for_account(phrase, "", i))
        used.append(crypto.public_key_to_address(pk))
    nonces = {a: 2 for a in used}
    st = types.SimpleNamespace(get_nonce=lambda a: nonces.get(a, 0), get_balance=lambda a: 0)
    node = FakeNode(st)
    assert w.discovery.step(node) == "scanned"
    assert [a["index"] for a in WalletFile.load(wallet.WALLET_FILE).accounts] == [0, 1, 3]
    assert not WalletFile.load(wallet.WALLET_FILE).scan_pending
    assert w.discovery.step(node) == "done"


# What the move must not break

def test_the_base_gas_wallet_is_sealed_under_the_new_key_and_still_opens(home, old_node, monkeypatch):
    import base_wallet
    legacy_kek = crypto.derive_kek(wallet.LEGACY_KEY_FILE, PASS)
    address = base_wallet.create(base_wallet.default_path(), legacy_kek)
    w = start(monkeypatch)
    new_kek = w.derive_kek(PASS)
    assert base_wallet.load_address(base_wallet.default_path()) == address
    base_wallet.decrypt_secret(base_wallet.default_path(), new_kek)        # opens now
    with pytest.raises(ValueError):
        base_wallet.decrypt_secret(base_wallet.default_path(), legacy_kek)  # and no longer under the old


def test_a_cut_short_start_still_reseals_the_gas_wallet_on_the_next_one(home, old_node, monkeypatch):
    import base_wallet
    legacy_kek = crypto.derive_kek(wallet.LEGACY_KEY_FILE, PASS)
    start(monkeypatch)                                    # wallet made, then "crashed"
    base_wallet.create(base_wallet.default_path(), legacy_kek)   # as if sealed under the old key
    w = start(monkeypatch)
    base_wallet.decrypt_secret(base_wallet.default_path(), w.derive_kek(PASS))


def test_without_the_library_an_old_key_keeps_running_as_it_was(home, old_node, monkeypatch, capsys):
    monkeypatch.setattr(falcon_seeded, "available", lambda: False)
    w = start(monkeypatch)
    assert isinstance(w, wallet.Wallet) and w.addr == old_node.addr
    assert not os.path.exists(wallet.WALLET_FILE) and os.path.exists(wallet.LEGACY_KEY_FILE)
    assert "make -C native/falcon_seeded" in capsys.readouterr().out


def test_a_key_file_path_is_refused_with_a_way_forward(home):
    (home / "k.json").write_text("{}")
    with pytest.raises(ValueError, match="key files are not read any more"):
        wallet.find_source(str(home / "k.json"))


def test_a_cut_short_start_after_the_sweep_was_stored_also_moves_what_arrived_since(home, old_node, monkeypatch):
    """Both the stored move and the old key survive a crash between the two
    steps. Once the move confirms, the key is still there to move any rest."""
    w = start(monkeypatch)
    node = old_node.node
    w.migration.step(node)                       # signs and stores, deletes the key
    sweep = WalletFile.load(wallet.WALLET_FILE).pending_sweep
    # put the old key file back, as if the process died before deleting it
    sk, pk = crypto.generate_keypair()
    old2 = wallet.Wallet(wallet.LEGACY_KEY_FILE, pk)
    save_key(wallet.LEGACY_KEY_FILE, sk, pk, PASS)
    m = migration.Migration(WalletFile.load(wallet.WALLET_FILE), old2,
                            old2.derive_kek(PASS), wallet.LEGACY_KEY_FILE)
    st = types.SimpleNamespace(get_nonce=lambda a: sweep["nonce"], get_balance=lambda a: 0)
    node.view.state = st
    assert m.step(node) == "confirmed"
    assert not m.done                            # the key is still there to sweep the rest
