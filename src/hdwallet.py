"""Recovery phrase to FALCON-512 account seeds, per the convention the
project site pins (wallets.html): BIP-39, then BIP-32 master key, then BIP-85
HEX at m/83696968'/128169'/48'/{21000000+account}', 48 bytes of entropy.

Pure derivation, no I/O. The 48-byte result is the seed for
falcon_seeded.keypair_from_seed. Do not change the path, the length or the
index offset: a wallet that does cannot restore other wallets' keys."""

import hashlib
import hmac
import secrets
import unicodedata

import crypto

ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
BASE_INDEX = 21_000_000
PATH = (83696968, 128169, 48)
PHRASE_LENGTHS = (12, 15, 18, 21, 24)

_INDEX = {w: i for i, w in enumerate(crypto._WORDLIST)}


def normalize_phrase(phrase):
    """Lower-cased words joined by single spaces."""
    return " ".join(phrase.lower().split())


def validate_phrase(phrase):
    """The normalized phrase, or ValueError saying what is wrong with it."""
    phrase = normalize_phrase(phrase)
    words = phrase.split(" ") if phrase else []
    if len(words) not in PHRASE_LENGTHS:
        raise ValueError("a recovery phrase has 12, 15, 18, 21 or 24 words")
    bits = ""
    for w in words:
        if w not in _INDEX:
            raise ValueError(f"'{w}' is not a recovery phrase word")
        bits += format(_INDEX[w], "011b")
    cs_len = len(words) * 11 // 33
    ent_len = len(bits) - cs_len
    entropy = int(bits[:ent_len], 2).to_bytes(ent_len // 8, "big")
    want = format(hashlib.sha256(entropy).digest()[0], "08b")[:cs_len]
    if bits[ent_len:] != want:
        raise ValueError("this recovery phrase has a typo: its checksum does not match")
    return phrase


def generate_phrase(words=12):
    """A new random BIP-39 phrase (12 to 24 words)."""
    if words not in PHRASE_LENGTHS:
        raise ValueError("words must be 12, 15, 18, 21 or 24")
    ent_len = words * 11 * 32 // 33
    entropy = secrets.token_bytes(ent_len // 8)
    cs_len = ent_len // 32
    bits = format(int.from_bytes(entropy, "big"), f"0{ent_len}b")
    bits += format(hashlib.sha256(entropy).digest()[0], "08b")[:cs_len]
    return " ".join(crypto._WORDLIST[int(bits[i:i + 11], 2)]
                    for i in range(0, len(bits), 11))


def bip39_seed(phrase, passphrase=""):
    nfkd = lambda s: unicodedata.normalize("NFKD", s).encode()
    return hashlib.pbkdf2_hmac("sha512", nfkd(phrase),
                               nfkd("mnemonic" + passphrase), 2048)


def master_key(seed):
    """(key, chain code) of the BIP-32 master node."""
    i = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    key = int.from_bytes(i[:32], "big")
    if key == 0 or key >= ORDER:
        raise ValueError("invalid master key")
    return key, i[32:]


def derive_hardened(key, chain, index):
    """BIP-32 hardened child of (key, chain) at `index` (below 2**31)."""
    data = b"\x00" + key.to_bytes(32, "big") + (index | 0x80000000).to_bytes(4, "big")
    i = hmac.new(chain, data, hashlib.sha512).digest()
    il = int.from_bytes(i[:32], "big")
    child = (il + key) % ORDER
    if il >= ORDER or child == 0:
        raise ValueError("invalid child key")
    return child, i[32:]


def falcon_seed_for_account(phrase, passphrase="", account=0):
    """The 48-byte FALCON-512 key generation seed for an account."""
    if not 0 <= account < 2**31 - BASE_INDEX:
        raise ValueError("account out of range")
    key, chain = master_key(bip39_seed(phrase, passphrase))
    for n in PATH + (BASE_INDEX + account,):
        key, chain = derive_hardened(key, chain, n)
    return hmac.new(b"bip-entropy-from-k", key.to_bytes(32, "big"),
                    hashlib.sha512).digest()[:48]
