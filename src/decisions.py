"""What the node decided when it started, and why.

A node settles a number of things from what it was given: who it runs as, when
it asks for a passphrase, which ports it uses, whether it shows a window. Each
is a choice made from an option, a variable or a default, and each can surprise
someone who did not know which one applied. This module turns the facts known
at start into plain statements with their reasons, for the Decisions page. It
decides nothing: the rules are in wallet.find_source and startup.plan, and what
this says must match them (tests/test_decisions.py).

Everything here is a pure function of values passed in, so every sentence can
be tested without starting a node.
"""

from collections import namedtuple

import startup

Entry = namedtuple("Entry", "what why")
Group = namedtuple("Group", "title entries")

PRECEDENCE = "The command line beats the environment, and the environment beats the folder."


def who(source, address, account=0, account_name="", account_given=False):
    """Who the node runs as, and what chose that."""
    where = source.where or "what was given"
    kind = source.kind
    if kind == "address":
        why = (f"{where} gave this address. It holds no key, so it can mine and relay "
               "but cannot sign anything.")
    elif kind == "key":
        why = f"{where} gave a key, and the address comes from it once it is opened."
    elif kind == "text":
        why = f"{where} gave an old single key, and the address is that key's."
    elif kind == "wallet":
        label = f" ({account_name})" if account_name else ""
        chose = (f"--account chose address {account}{label}" if account_given
                 else f"no --account was given, so it is address {account}{label}")
        why = f"{where} is in this folder, and {chose}."
    elif kind == "legacy":
        why = (f"{where} was found, so this node moved to a new recovery-phrase wallet. "
               "The old address's funds follow once it has synced.")
    else:
        why = "Nothing was here, so a new recovery-phrase wallet was made."
    return [Entry(f"Mines and builds as {address}", why)]


def passphrase(source, how, env_passphrase=False, window=False, phrase_unconfirmed=False):
    """When the passphrase is asked for, and why that way."""
    kind = source.kind
    if how == startup.WATCH:
        return [Entry("No passphrase is ever asked for",
                      "An address holds no key, so there is nothing to unlock. This node cannot "
                      "send, post, vote or answer fee requests.")]
    if how == startup.DEFER:
        place = ("The window is up, so it started at once" if window
                 else "No terminal is attached to ask on, so it started at once")
        return [Entry("The passphrase is asked for on the private pages, not at start",
                      f"This node's address can be read without it. {place}, and what needs "
                      "the key (answering fee requests, finishing a wallet move) starts when "
                      "you enter it.")]
    if env_passphrase:
        return [Entry("The passphrase came from $LAPSECOIN_PASSPHRASE",
                      "It was set, so there was nothing to ask.")]
    if kind == "key":
        reason = "An lc1- key keeps its address sealed until it is opened."
    elif kind == "legacy":
        reason = "Moving an old key creates a new wallet and shows you its new recovery phrase."
    elif kind == "new":
        reason = "A new wallet needs a passphrase, and shows you its recovery phrase."
    elif phrase_unconfirmed:
        reason = "The recovery phrase was never confirmed, so it is shown again first."
    else:
        reason = "A terminal is attached, so it asks there."
    return [Entry("The passphrase was asked for before starting", reason)]


def passed_over(notes):
    """What was given but not used, from wallet.find_source and the startup
    notes, each "X is ignored: reason". Where the reason is that something was
    given on the command line, the precedence rule is the whole explanation."""
    out = []
    for note in notes:
        what, _, why = note.partition(": ")
        if why.endswith("was given"):
            why += ". " + PRECEDENCE
        out.append(Entry(what, why))
    return out


def about_local_wallet(notes):
    """What matched the wallet file in this folder (wallet.notes_about_local_wallet)."""
    return [Entry(note, "Nothing was overridden: what you gave is what runs.") for note in notes]


def network(*, public_port, requested_port, port_how, private_port, private_how,
            peers=(), relay=True, public_url=None, public_url_how=""):
    out = []
    if public_port != requested_port:
        why = (f"Port {requested_port} (from {port_how}) was already in use, here or by another "
               "node on this network, so it moved up.")
    else:
        why = f"From {port_how}."
    out.append(Entry(f"Peers and the public explorer use port {public_port}", why))
    out.append(Entry(f"The wallet pages are on 127.0.0.1:{private_port}",
                     "From --private-port." if private_how == "--private-port"
                     else "The public port plus 2, since --private-port was not given."))
    if peers:
        out.append(Entry(f"Tries {len(peers)} node{'s' if len(peers) != 1 else ''} you named first: "
                         + ", ".join(peers), "From --peer."))
    else:
        out.append(Entry("Finds peers on its own: LAN broadcast, saved peers and the DHT",
                         "No --peer was given."))
    out.append(Entry("Passes light clients' sealed requests on" if relay
                     else "Does not pass light clients' sealed requests on",
                     "On by default." if relay else "--no-relay was given."))
    if public_url:
        out.append(Entry(f"Advertises {public_url} for light clients", f"From {public_url_how}."))
    return out


def window_and_updates(*, window, no_gui, update_check, update_url_how=""):
    out = []
    if window:
        out.append(Entry("Shows the desktop window",
                         "A display is available and --no-gui was not given."))
    else:
        out.append(Entry("Runs in the console, with no window",
                         "--no-gui was given." if no_gui else "No display was found."))
    out.append(Entry("Checks for new releases" if update_check else "Does not check for new releases",
                     "On by default." if update_check else "--no-update-check was given."))
    return out


def data(*, db_path, db_how, log_level, log_how):
    return [Entry(f"Keeps the chain in {db_path}", f"From {db_how}."),
            Entry(f"Logs at {log_level}", f"From {log_how}.")]


class Decisions:
    """The groups of decisions, in the order they are shown. Groups with
    nothing in them are left out."""

    ORDER = ("Who this node is", "Passphrase and signing", "Passed over",
             "About the wallet here", "Network", "Window and updates", "Data and logs")

    def __init__(self):
        self._groups = {}

    def add(self, title, entries):
        self._groups.setdefault(title, []).extend(entries)

    def groups(self):
        return [Group(t, self._groups[t]) for t in self.ORDER if self._groups.get(t)]
