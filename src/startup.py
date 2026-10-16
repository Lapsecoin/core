"""Who a node runs as, and when it asks for a passphrase.

What the node is given decides who it is (wallet.find_source: --address, then
--key, then $LAPSECOIN_ADDRESS, then $LAPSECOIN_KEY, then the wallet file, then
an old key file, then nothing, which makes a new wallet). This module decides
the other half: whether the node starts straight away, starts and asks later,
or has to ask first. It is a pure function of what is known at start, so every
case is a line in tests/test_startup.py.

  what the node runs as              window            terminal, with a        no terminal
                                                       terminal (a service)
  an address (no key)                starts, no ask    starts, no ask          starts, no ask
  a wallet file, phrase confirmed    starts, asks on   asks first              starts, asks on
  or an old single-key text          the private pages                         the private pages
  an lc1- key                        asks first        asks first              needs $LAPSECOIN_PASSPHRASE
  an old key file to move,
  a new wallet, or a wallet whose
  phrase was never confirmed         asks first        asks first              needs $LAPSECOIN_PASSPHRASE

"Asks first" is a dialog in the window and a prompt in the terminal. If
$LAPSECOIN_PASSPHRASE is set there is nothing to ask, so the node unlocks at
start and never defers. A start that has to ask and cannot (no window, no
terminal, no passphrase in the environment) stops with a message that says so,
rather than hanging on a prompt nobody can see.
"""

from collections import namedtuple

import wallet as wallet_mod

WATCH = "watch"     # no key, nothing to ask now or later
DEFER = "defer"     # run now as the address; the passphrase is asked for on the private pages
ASK = "ask"         # ask for the passphrase (or take it from the environment) before starting

# wallet is the node's wallet for WATCH and DEFER (nothing can sign yet), and
# None for ASK, where the passphrase is needed to make it.
Plan = namedtuple("Plan", "source how wallet")


def plan(source, account=0, *, gui, tty, env_passphrase):
    """The Plan for `source`, or ValueError saying what is missing.

    account: which address of a wallet file the node runs as.
    gui: the window is available. tty: a terminal is attached (stdin).
    env_passphrase: $LAPSECOIN_PASSPHRASE is set.

    Whether a start can wait for its passphrase is decided in one place,
    wallet.open_locked: it can when the address is readable without it and
    nothing has to be shown first."""
    wallet = wallet_mod.open_locked(source, account)
    if source.kind == "address":
        return Plan(source, WATCH, wallet)
    if wallet is not None:
        if env_passphrase:
            return Plan(source, ASK, None)
        if gui or not tty:
            return Plan(source, DEFER, wallet)
        return Plan(source, ASK, None)
    if gui or tty or env_passphrase:
        return Plan(source, ASK, None)
    raise ValueError("this start needs a passphrase and cannot ask for one: there is no "
                     "window and no terminal. Set LAPSECOIN_PASSPHRASE, or run it in a terminal.")
