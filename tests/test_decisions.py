"""The Decisions page tells the truth about a start: what each statement says
has to match the rule that made the decision (wallet.find_source,
startup.plan), and the page must show it, safely, under Configuration."""

import pytest

import api
import decisions
import peerpool as peerpool_mod
import startup
import wallet as wallet_mod
from chainstate import ChainState
from tests.test_api import _FakeNode

ADDR = "tree.mention.pride.mix.industry.lottery.can.swap.cram.share.park.wide"
LC1 = "lc1-" + "x" * 40


def source(kind, where, value="x"):
    s = wallet_mod.Source(kind, value)
    s.where = where
    return s


def text(entries):
    return " ".join(f"{e.what} {e.why}" for e in entries)


# Where a source came from is recorded for every way of choosing one
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (wallet_mod.ADDRESS_ENV, wallet_mod.KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_a_source_remembers_what_chose_it(home, monkeypatch):
    assert wallet_mod.find_source(address_text=ADDR).where == "--address"
    assert wallet_mod.find_source(LC1).where == "--key"
    open(wallet_mod.WALLET_FILE, "w").write("{}")
    assert wallet_mod.find_source(account_given=True).where == "--account"
    assert wallet_mod.find_source().where == "lapsecoin_wallet.json here"
    monkeypatch.setenv(wallet_mod.ADDRESS_ENV, ADDR)
    assert wallet_mod.find_source().where == "$LAPSECOIN_ADDRESS"
    monkeypatch.delenv(wallet_mod.ADDRESS_ENV)
    monkeypatch.setenv(wallet_mod.KEY_ENV, LC1)
    assert wallet_mod.find_source().where == "$LAPSECOIN_KEY"


def test_the_origin_does_not_change_what_a_source_equals(home):
    assert wallet_mod.find_source(address_text=ADDR) == wallet_mod.Source("address", ADDR)


# What each statement says
def test_who_says_what_chose_the_address():
    [e] = decisions.who(source("address", "--address", ADDR), ADDR)
    assert ADDR in e.what and "--address gave this address" in e.why and "cannot sign" in e.why
    [e] = decisions.who(source("wallet", "lapsecoin_wallet.json here"), ADDR, 0)
    assert "no --account was given, so it is address 0" in e.why
    [e] = decisions.who(source("wallet", "--account"), ADDR, 2, "rig", account_given=True)
    assert "--account chose address 2 (rig)" in e.why


@pytest.mark.parametrize("kind, how, env, window, unconfirmed, expect", [
    ("address", startup.WATCH, False, True,  False, "No passphrase is ever asked for"),
    ("wallet",  startup.DEFER, False, True,  False, "private pages"),
    ("wallet",  startup.DEFER, False, False, False, "No terminal is attached"),
    ("wallet",  startup.ASK,   True,  True,  False, "$LAPSECOIN_PASSPHRASE"),
    ("key",     startup.ASK,   False, True,  False, "sealed"),
    ("legacy",  startup.ASK,   False, True,  False, "new recovery phrase"),
    ("new",     startup.ASK,   False, True,  False, "shows you its recovery phrase"),
    ("wallet",  startup.ASK,   False, True,  True,  "never confirmed"),
    ("wallet",  startup.ASK,   False, False, False, "terminal is attached"),
])
def test_the_passphrase_statement_gives_the_reason_the_plan_acted_on(kind, how, env, window, unconfirmed, expect):
    out = text(decisions.passphrase(source(kind, "x"), how, env, window, unconfirmed))
    assert expect in out


def test_the_network_statement_says_why_a_port_moved_and_where_it_came_from():
    moved = text(decisions.network(public_port=8334, requested_port=8333, port_how="the default",
                                   private_port=8336, private_how=""))
    assert "8334" in moved and "Port 8333 (from the default) was already in use" in moved
    assert "plus 2" in moved
    kept = text(decisions.network(public_port=9000, requested_port=9000, port_how="--port",
                                  private_port=9100, private_how="--private-port"))
    assert "From --port." in kept and "already in use" not in kept and "From --private-port." in kept


def test_peers_and_relay_and_public_url_are_stated():
    out = text(decisions.network(public_port=1, requested_port=1, port_how="--port", private_port=3,
                                 private_how="", peers=["1.2.3.4:8333", "5.6.7.8:8333"], relay=False,
                                 public_url="https://node.example", public_url_how="--public-url"))
    assert "2 nodes you named first: 1.2.3.4:8333, 5.6.7.8:8333" in out
    assert "Does not pass light clients" in out and "--no-relay was given" in out
    assert "https://node.example" in out
    alone = text(decisions.network(public_port=1, requested_port=1, port_how="--port", private_port=3,
                                   private_how=""))
    assert "Finds peers on its own" in alone and "Passes light clients" in alone


def test_the_window_and_update_statements_name_the_option_or_the_reason():
    assert "--no-gui was given" in text(decisions.window_and_updates(window=False, no_gui=True, update_check=False))
    assert "No display was found" in text(decisions.window_and_updates(window=False, no_gui=False, update_check=True))
    assert "A display is available" in text(decisions.window_and_updates(window=True, no_gui=False, update_check=True))
    assert "--no-update-check was given" in text(decisions.window_and_updates(window=True, no_gui=False, update_check=False))


def test_empty_groups_are_left_out_and_the_rest_keep_their_order():
    d = decisions.Decisions()
    d.add("Network", decisions.data(db_path="/x", db_how="--db", log_level="INFO", log_how="the default"))
    d.add("Who this node is", decisions.who(source("address", "--address", ADDR), ADDR))
    d.add("Passed over", [])
    assert [g.title for g in d.groups()] == ["Who this node is", "Network"]


# The page
class DecidedNode(_FakeNode):
    def __init__(self, groups=None):
        super().__init__(ChainState.from_genesis(), addr=ADDR)
        if groups is not None:
            self.decisions = groups


def client_for(node):
    return api.create_private_app(node, peerpool_mod.PeerPool()).test_client()


def test_the_page_lists_every_group_with_its_reasons():
    d = decisions.Decisions()
    d.add("Who this node is", decisions.who(source("address", "--address", ADDR), ADDR))
    d.add("Passed over", decisions.passed_over(["$LAPSECOIN_KEY is ignored: --address was given"]))
    html = client_for(DecidedNode(d)).get("/decisions").get_data(as_text=True)
    assert "Who this node is" in html and "Passed over" in html
    assert ADDR in html and "--address gave this address" in html
    assert "$LAPSECOIN_KEY is ignored" in html and "The command line beats the environment" in html


def test_what_a_person_typed_is_escaped():
    d = decisions.Decisions()
    d.add("Network", decisions.network(public_port=1, requested_port=1, port_how="--port", private_port=3,
                                       private_how="", peers=['<script>alert(1)</script>:1']))
    html = client_for(DecidedNode(d)).get("/decisions").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_a_node_that_recorded_nothing_still_shows_a_page():
    html = client_for(DecidedNode()).get("/decisions").get_data(as_text=True)
    assert "Nothing was recorded" in html


def test_the_menu_has_a_configuration_group_with_settings_and_decisions():
    html = client_for(DecidedNode()).get("/decisions").get_data(as_text=True)
    menu = html.split("Configuration<svg", 1)[1].split('role="menu">', 1)[1].split("</div>", 1)[0]
    assert 'href="/settings"' in menu and 'href="/decisions"' in menu
    assert 'class="nav-group active"' in html          # the page we are on lights its group


def test_settings_still_lights_the_same_group():
    html = client_for(DecidedNode()).get("/settings").get_data(as_text=True)
    assert 'class="nav-group active"' in html and 'href="/decisions"' in html


def test_the_public_port_does_not_serve_it():
    pub = api.create_app(DecidedNode(), peerpool_mod.PeerPool()).test_client()
    assert pub.get("/decisions").status_code == 404
