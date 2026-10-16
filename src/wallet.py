"""A wallet: where the node's key comes from, its public key and address, and
the one place a transaction gets built and signed.

Deliberately knows nothing about chain state. The nonce is passed in by
whoever can see it (a full node reads it from its own state and mempool).

Three kinds of wallet, all with the same small surface (pk, pk_hex, addr,
derive_kek, sign_tx):

  PhraseWallet    lapsecoin_wallet.json, a recovery phrase and its accounts
                  (walletfile.py). The node runs as one account of it.
  SeedKeyWallet   an lc1- text from --export, one account's key and nothing
                  else (shortkey.py). Never written to disk.
  Wallet          the old single-key file or its pasted text. Kept so an old
                  key still runs; a node whose key is a lapsecoin_key.json
                  file moves to a PhraseWallet at startup (migration.py).

--key and $LAPSECOIN_KEY take key text only, never a path."""

import getpass
import json
import logging
import os
import sys
from collections import namedtuple

import crypto
import falcon_seeded
import hdwallet
import shortkey
import tx as tx_mod
from walletfile import WALLET_FILE, WalletFile, WalletFileError

# Same logger name main.py logs under, so the startup lines read the same
# whichever program created or loaded the key.
log = logging.getLogger("ec.main")


# The old single-key file. Nothing writes this name any more; finding it in
# the data directory is what tells a node to move to a recovery phrase.
LEGACY_KEY_FILE = "lapsecoin_key.json"
KEY_ENV = "LAPSECOIN_KEY"
PASSPHRASE_ENV = "LAPSECOIN_PASSPHRASE"
EXPORT_PASSPHRASE_ENV = "LAPSECOIN_EXPORT_PASSPHRASE"
MIN_PASSPHRASE = 8


# ---------------------------------------------------------------------------
# The wallets
# ---------------------------------------------------------------------------

class _Signer:
    """Building and signing a transaction, shared by every kind of wallet.
    A kind supplies pk, pk_hex, addr, derive_kek and secret_key."""

    def sign_tx(self, outputs, nonce, fee, memo="", passphrase=None, kek=None):
        """Build and sign a transaction from this wallet. Supply exactly
        one of passphrase or kek. Returns (tx_dict, fee)."""
        if kek is None:
            if not passphrase:
                raise ValueError("passphrase or kek is required")
            kek = self.derive_kek(passphrase)
        sk = self.secret_key(kek)
        try:
            t = tx_mod.create(self.addr, self.pk_hex, outputs, nonce, fee, sk, memo=memo)
        finally:
            del sk
        return t, fee


class Wallet(_Signer):
    """The old single-key file."""

    def __init__(self, keyfile, public_key):
        self.keyfile = keyfile
        self.pk      = public_key
        self.pk_hex  = public_key.hex()
        self.addr    = crypto.public_key_to_address(public_key)

    def derive_kek(self, passphrase):
        return crypto.derive_kek(self.keyfile, passphrase)

    def secret_key(self, kek):
        return crypto.decrypt_secret_key(self.keyfile, kek=kek)


class TextWallet(_Signer):
    """An old single key held as pasted text, never written to disk."""

    def __init__(self, data):
        self._data   = data
        self.pk      = bytes.fromhex(data["public_key"])
        self.pk_hex  = data["public_key"]
        self.addr    = crypto.public_key_to_address(self.pk)

    def derive_kek(self, passphrase):
        return crypto.derive_kek_data(self._data, passphrase)

    def secret_key(self, kek):
        return crypto.decrypt_secret_key_data(self._data, kek)


class SeedKeyWallet(_Signer):
    """One account from an lc1- text. The FALCON key is derived from the
    sealed seed each time it is needed."""

    def __init__(self, parts, kek):
        self._parts = parts
        seed = shortkey.open_seed(parts, kek)
        try:
            sk, self.pk = falcon_seeded.keypair_from_seed(seed)
        finally:
            del seed
        del sk
        self.pk_hex = self.pk.hex()
        self.addr   = crypto.public_key_to_address(self.pk)

    def derive_kek(self, passphrase):
        return shortkey.derive_kek(self._parts, passphrase)

    def secret_key(self, kek):
        seed = shortkey.open_seed(self._parts, kek)
        try:
            sk, _pk = falcon_seeded.keypair_from_seed(seed)
        finally:
            del seed
        return sk


class PhraseWallet(_Signer):
    """One account of a recovery-phrase wallet. `walletfile` is the whole
    wallet; this object is the account the node signs as."""

    def __init__(self, walletfile, account=0):
        try:
            self.pk = walletfile.public_key(account)
        except KeyError:
            raise ValueError(f"account {account} is not in this wallet "
                             "(--new-address adds one)")
        self.walletfile = walletfile
        self.account    = account
        self.pk_hex     = self.pk.hex()
        self.addr       = walletfile.address(account)
        # Background jobs the node runs once it is up: moving an old key's
        # funds here, and finding a restored phrase's other addresses.
        self.migration  = None
        self.discovery  = None

    def derive_kek(self, passphrase):
        return self.walletfile.derive_kek(passphrase)

    def secret_key(self, kek):
        return self.walletfile.secret_key(kek, self.account)


# ---------------------------------------------------------------------------
# Where the key comes from
# ---------------------------------------------------------------------------

Source = namedtuple("Source", "kind value")
# kinds: "key" (lc1 text), "text" (old single-key text), "wallet" (the phrase
# file), "legacy" (an old lapsecoin_key.json to move), "new" (nothing yet).


def _legacy_text(text):
    """The old --export JSON, or None if `text` is not that."""
    lines = [ln.strip() for ln in text.splitlines() if ln.lstrip().startswith("{")]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
        for field in ("public_key", "ciphertext", "salt"):
            if not isinstance(data[field], str):
                raise TypeError(field)
    except (ValueError, KeyError, TypeError):
        raise ValueError("that is not a LapseCoin key")
    return data


def find_source(key_text=None, wallet_file=WALLET_FILE, legacy_file=LEGACY_KEY_FILE):
    """What this start-up should run as. Key text (--key, else $LAPSECOIN_KEY)
    wins; otherwise the wallet file; otherwise an old key file to move;
    otherwise there is nothing yet and a wallet gets created."""
    text = (key_text or os.environ.get(KEY_ENV) or "").strip()
    if text:
        if shortkey.looks_like(text):
            return Source("key", text)
        data = _legacy_text(text)
        if data is None:
            hint = ""
            if os.path.isfile(text):
                hint = (f" {text} is a file, and key files are not read any more: put it in this "
                        f"node's folder as {LEGACY_KEY_FILE} to move it to a recovery phrase")
            raise ValueError("--key takes the key text that --export prints "
                             "(it starts with lc1-)." + hint)
        return Source("text", data)
    if os.path.exists(wallet_file):
        return Source("wallet", wallet_file)
    if os.path.exists(legacy_file):
        return Source("legacy", legacy_file)
    return Source("new", wallet_file)


def unlock(source, passphrase, account=0):
    """(wallet, kek) for a source that already exists. ValueError for a wrong
    passphrase or a key text that is cut off."""
    kind, value = source
    if kind == "key":
        parts = shortkey.decode(value)
        kek = shortkey.derive_kek(parts, passphrase)
        return SeedKeyWallet(parts, kek), kek
    if kind == "text":
        w = TextWallet(value)
        kek = w.derive_kek(passphrase)
        crypto.decrypt_secret_key_data(value, kek)
        return w, kek
    if kind == "wallet":
        wf = WalletFile.load(value)
        kek = wf.derive_kek(passphrase)
        wf.verify(kek)
        w = PhraseWallet(wf, account)
        _attach_jobs(w, kek, passphrase)
        return w, kek
    if kind == "legacy":
        w = Wallet(value, crypto.load_pubkey(value))
        kek = w.derive_kek(passphrase)
        crypto.decrypt_secret_key(value, kek=kek)
        return w, kek
    raise ValueError("nothing to unlock")


def _attach_jobs(w, kek, passphrase, legacy_file=LEGACY_KEY_FILE):
    """A wallet that still has an old key to move (a start that was cut short
    leaves the old file beside the new one), a move waiting to confirm, or a
    restore to scan gets the job that finishes it."""
    from migration import Discovery, Migration
    wf = w.walletfile
    legacy = legacy_kek = None
    if os.path.exists(legacy_file):
        try:
            legacy = Wallet(legacy_file, crypto.load_pubkey(legacy_file))
            legacy_kek = legacy.derive_kek(passphrase)
            crypto.decrypt_secret_key(legacy_file, kek=legacy_kek)
        except (ValueError, OSError, KeyError):
            log.warning("[startup] %s is there but does not open with this passphrase; "
                        "leaving it alone", legacy_file)
            legacy = legacy_kek = None
    if legacy is not None:
        _reseal_gas_wallet(legacy_kek, kek)
    if legacy is not None or wf.pending_sweep:
        w.migration = Migration(wf, legacy, legacy_kek, legacy_file if legacy else None)
    if wf.scan_pending:
        w.discovery = Discovery(wf, kek)


def _reseal_gas_wallet(old_kek, new_kek):
    """The Base gas wallet is sealed under the node's kek, which changes when
    the node moves to a recovery phrase; seal it under the new one."""
    try:
        import base_wallet
        base_wallet.reseal(base_wallet.default_path(), old_kek, new_kek)
    except Exception as e:
        log.warning("[startup] could not move the Base gas wallet to the new wallet key: %s", e)


def begin_migration(passphrase, legacy, legacy_kek, legacy_file=LEGACY_KEY_FILE,
                    path=WALLET_FILE):
    """An old key becomes a recovery-phrase wallet, under the same passphrase.
    Returns (wallet, kek, phrase) like create_wallet; the caller shows the
    phrase. The node then runs as the new account 0, and the returned
    wallet's `migration` job moves the old address's funds there."""
    from migration import Migration
    w, kek, phrase = create_wallet(passphrase, path)
    _reseal_gas_wallet(legacy_kek, kek)
    w.migration = Migration(w.walletfile, legacy, legacy_kek, legacy_file)
    log.info("[startup] moving the old key %s to the new address %s", legacy.addr, w.addr)
    return w, kek, phrase


NO_SEEDED_LIBRARY = (
    "This node still runs on its old key because the seeded FALCON library is not installed here, "
    "which a recovery-phrase wallet needs. Build it with 'make -C native/falcon_seeded' (needs a C "
    "compiler), or re-run the install script, then restart to move to a recovery phrase.")

MIGRATION_NOTICE = (
    "This node's old single key has been replaced by a recovery-phrase wallet.\n"
    "Its funds move to the new address once the node has synced, and the node\n"
    "now runs as the new address.")


def create_wallet(passphrase, path=WALLET_FILE, phrase=None):
    """Write a new wallet. A fresh 12-word phrase unless `phrase` is given
    (a restore). Returns (wallet, kek, phrase); the caller must show the
    phrase and then call walletfile.mark_phrase_shown(). The file is
    written first and flagged as not yet shown, so a crash before the user
    has seen the phrase shows it again at the next start."""
    new = phrase is None
    phrase = hdwallet.generate_phrase(12) if new else hdwallet.validate_phrase(phrase)
    wf = WalletFile.create(path, phrase, passphrase, phrase_shown=not new,
                           scan_pending=not new)
    kek = wf.derive_kek(passphrase)
    log.info("[startup] wallet created  file=%s", path)
    w = PhraseWallet(wf, 0)
    if not new:
        from migration import Discovery
        w.discovery = Discovery(wf, kek)
    return w, kek, phrase


# ---------------------------------------------------------------------------
# The console flow (the window has its own, in gui.py, over the same calls)
# ---------------------------------------------------------------------------

def resolve_passphrase(prompt):
    env_pass = os.environ.get(PASSPHRASE_ENV)
    if env_pass:
        return env_pass
    return getpass.getpass(prompt)


def prompt_new_passphrase(first=None, label="New passphrase: "):
    while True:
        p1 = first if first else getpass.getpass(label)
        first = None
        if len(p1) < MIN_PASSPHRASE:
            print(f"Passphrase must be at least {MIN_PASSPHRASE} characters.")
            continue
        p2 = getpass.getpass("Confirm passphrase: ")
        if p1 == p2:
            return p1
        print("Passphrases do not match.")


def show_phrase_console(phrase, out=None):
    """Print the recovery phrase once. On a terminal, wait until it has been
    written down. It goes to the screen only, never to the log, and without a
    terminal (a service, a container) this one print is the only place it
    ever appears."""
    out = out or sys.stdout
    words = phrase.split()
    print("", file=out)
    print("=" * 64, file=out)
    print("  YOUR RECOVERY PHRASE", file=out)
    print("  Write these words down. They restore every address of this", file=out)
    print("  wallet on any machine, and they are shown only this once.", file=out)
    print("", file=out)
    for row in range(0, len(words), 4):
        print("  " + "   ".join(f"{i + 1:>2}. {w:<9}" for i, w in
                                 enumerate(words[row:row + 4], start=row)), file=out)
    print("=" * 64, file=out)
    out.flush()
    if sys.stdin and sys.stdin.isatty() and out is sys.stdout:
        input("Press Enter once you have saved it. ")
        # Clear it off a terminal that keeps scrollback: best effort.
        print("\033[2J\033[H", end="", file=out)
    else:
        print("(No terminal to wait on: save the phrase now, it is not shown again.)",
              file=out)


def startup_console(key_text=None, account=0):
    """Console start-up: returns (wallet, kek, passphrase). Exits with a
    message on a wrong passphrase or a bad key."""
    try:
        source = find_source(key_text)
    except ValueError as e:
        sys.exit(f"Error: --key: {e}")

    if source.kind == "new":
        print("No wallet found. Creating a new one.")
        passphrase = resolve_passphrase("New passphrase: ")
        if not os.environ.get(PASSPHRASE_ENV):
            passphrase = prompt_new_passphrase(passphrase)
        try:
            wallet, kek, phrase = create_wallet(passphrase, source.value)
        except RuntimeError as e:
            sys.exit(f"Error: {e}")
        show_phrase_console(phrase)
        del phrase
        wallet.walletfile.mark_phrase_shown()
        log.info("[startup] address=%s", wallet.addr)
        return wallet, kek, passphrase

    passphrase = resolve_passphrase("Passphrase: ")
    try:
        wallet, kek = unlock(source, passphrase, account)
    except (ValueError, WalletFileError, RuntimeError) as e:
        sys.exit(f"Error: {e}")
    if source.kind == "legacy" and not falcon_seeded.available():
        # An old key keeps working as it always did until the library is there.
        log.warning("[startup] %s", NO_SEEDED_LIBRARY)
        print(NO_SEEDED_LIBRARY)
        return wallet, kek, passphrase
    if source.kind == "legacy":
        legacy, legacy_kek = wallet, kek
        wallet, kek, phrase = begin_migration(passphrase, legacy, legacy_kek, source.value)
        print(MIGRATION_NOTICE)
        show_phrase_console(phrase)
        del phrase
        wallet.walletfile.mark_phrase_shown()
        log.info("[startup] address=%s", wallet.addr)
        return wallet, kek, passphrase
    if source.kind == "wallet" and not wallet.walletfile.phrase_shown:
        # A crash before the phrase was acknowledged: show it again.
        phrase = wallet.walletfile.phrase(kek)
        show_phrase_console(phrase)
        del phrase
        wallet.walletfile.mark_phrase_shown()
    log.info("[startup] key loaded  kind=%s", source.kind)
    return wallet, kek, passphrase


# ---------------------------------------------------------------------------
# Commands that run and exit (--export, --new-address, --restore)
# ---------------------------------------------------------------------------

def resolve_account(spec, wallet_file=WALLET_FILE):
    """--account as a number: a number as typed, or the name given to an address
    on the Addresses page (case does not matter). ValueError says what is wrong
    and lists the names there are."""
    if spec is None or str(spec).strip() == "":
        return 0
    text = str(spec).strip()
    if text.isdigit():
        return int(text)
    if not os.path.exists(wallet_file):
        raise ValueError(f"there is no wallet here to look the name '{text}' up in; use the number")
    accounts = WalletFile.load(wallet_file).accounts
    hits = [a["index"] for a in accounts if a.get("label", "").strip().lower() == text.lower()]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(f"{a['label']} ({a['index']})" for a in accounts if a.get("label"))
    if hits:
        raise ValueError(f"more than one address is named '{text}'; use its number")
    raise ValueError(f"no address is named '{text}'" + (f"; the names are: {names}" if names else
                                                         "; none of the addresses has a name yet"))


def _need_wallet(source, what):
    if source.kind == "wallet":
        return
    messages = {
        "new": f"there is no wallet here to {what}",
        "key": "this node runs on a single exported key; run this where the recovery-phrase wallet is",
        "text": "this node runs on an old single key; it has no recovery phrase",
        "legacy": "this node still has an old key file; start the node once to move it to a recovery phrase",
    }
    sys.exit(f"Error: {messages[source.kind]}")


def _unlocked_wallet_file(key_text, what):
    try:
        source = find_source(key_text)
    except ValueError as e:
        sys.exit(f"Error: --key: {e}")
    _need_wallet(source, what)
    passphrase = resolve_passphrase("Wallet passphrase: ")
    try:
        wf = WalletFile.load(source.value)
        kek = wf.derive_kek(passphrase)
        wf.verify(kek)
    except (ValueError, WalletFileError) as e:
        sys.exit(f"Error: {e}")
    return wf, kek, passphrase


def _account_for_export(wf, kek, spec):
    """(index, created) for `--export SPEC`: an address's number, or its name.
    A name no address has yet makes a new address with that name, so
    `--export rig1` and `--export rig2` give two rigs an address each, kept in
    this wallet where their balances can be watched."""
    text = str(spec).strip() if spec is not None else ""
    if text == "":
        return 0, False
    if text.isdigit():
        return int(text), False
    hits = [a["index"] for a in wf.accounts if a.get("label", "").strip().lower() == text.lower()]
    if len(hits) == 1:
        return hits[0], False
    if hits:
        sys.exit(f"Error: more than one address is named '{text}'; use its number")
    entry = wf.new_account(kek)
    wf.set_label(entry["index"], text)
    return entry["index"], True


def cmd_export(key_text=None, account=0):
    """Print one account's key as lc1- text, sealed under a NEW passphrase
    (never the wallet's), for --key or $LAPSECOIN_KEY on another machine.
    `account` is a number or a name; a name nobody has yet creates the address."""
    try:
        source = find_source(key_text)
    except ValueError as e:
        sys.exit(f"Error: --key: {e}")
    if source.kind == "legacy":
        # An old key file has no seed to shorten: print it as it always was
        # (still encrypted under its own passphrase).
        with open(source.value) as f:
            print(json.dumps(json.load(f), separators=(",", ":")))
        return
    wf, kek, passphrase = _unlocked_wallet_file(key_text, "export from")
    new_pass = os.environ.get(EXPORT_PASSPHRASE_ENV)
    if new_pass:
        if new_pass == passphrase:
            sys.exit("Error: the exported key needs a passphrase different from the wallet's")
    else:
        while True:
            new_pass = prompt_new_passphrase(label="Passphrase for the exported key: ")
            if new_pass != passphrase:
                break
            print("Use a different passphrase from the wallet's, so the box never holds it.")
    index, created = _account_for_export(wf, kek, account)
    try:
        text = wf.export_account_key(kek, index, new_pass, passphrase)
    except (ValueError, KeyError) as e:
        sys.exit(f"Error: {e}" if isinstance(e, ValueError) else f"Error: account {index} is not in this wallet")
    if created:
        print(f"Created address {index} named '{wf.account(index)['label']}': {wf.address(index)}",
              file=sys.stderr)
    print(text)


def cmd_new_address(key_text=None):
    wf, kek, _passphrase = _unlocked_wallet_file(key_text, "add an address to")
    entry = wf.new_account(kek)
    print(f"Account {entry['index']}: {entry['address']}")


def cmd_restore(path=WALLET_FILE, legacy_file=LEGACY_KEY_FILE):
    """Rebuild a wallet from a recovery phrase typed (hidden) or piped in."""
    if os.path.exists(path) or os.path.exists(legacy_file):
        sys.exit("Error: a wallet already exists here; restore into an empty folder")
    if sys.stdin.isatty():
        phrase = getpass.getpass("Recovery phrase: ")
    else:
        phrase = sys.stdin.readline()
    try:
        phrase = hdwallet.validate_phrase(phrase)
    except ValueError as e:
        sys.exit(f"Error: {e}")
    passphrase = resolve_passphrase("New passphrase: ")
    if not os.environ.get(PASSPHRASE_ENV):
        passphrase = prompt_new_passphrase(passphrase)
    wallet, _kek, _phrase = create_wallet(passphrase, path, phrase=phrase)
    print(f"Restored. Account 0: {wallet.addr}")
    print("The node finds the phrase's other addresses from the chain when it starts.")
