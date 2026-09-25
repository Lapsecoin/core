"""Deterministic per-address TLS identity for the relay fallback.

Syncthing's real relay protocol authenticates a joined client by hashing
the TLS client certificate it presented (SHA-256 of the DER bytes), and
expects a ConnectRequest to name that same hash as the target it wants.
That scheme assumes both sides already know each other's device ID from a
prior pairing step (a config entry, a scanned QR code); our two nodes have
never exchanged anything like that, they only know each other's addr from
DHT, which is exactly what the relay fallback exists to work around in the
first place.

The fix: make the certificate a *public, deterministic* function of
(genesis_hash, addr) instead of a private secret. Anyone who knows a
node's addr (which, on this network, means anyone) can independently
regenerate the exact certificate that node would present when joining a
relay for this genesis, and therefore its device ID, without either side
ever having to tell the other. This is not authentication (there is no
secret in it, and it isn't meant to be one) — it's a public rendezvous
label, nothing more. Actual peer trust still happens one layer up, at the
same genesis-hash PING/PONG handshake a direct connection already goes
through; a relay session carries bytes, it doesn't vouch for anyone.

Ed25519 is used because its signatures are deterministic per RFC 8032 (no
per-signature randomness to control for), and a self-signed cert with a
fixed serial number and a fixed, non-expiring validity window means the
resulting DER bytes are a pure function of the input seed with nothing
time- or randomness-dependent left to make two independent runs diverge.
"""

import datetime
import hashlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

# Fixed so the resulting cert DER is deterministic given only the seed.
# Values themselves are arbitrary; changing them changes every device ID
# this module has ever produced, so don't.
_FIXED_SERIAL     = 1
_FIXED_NOT_BEFORE = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_FIXED_NOT_AFTER  = datetime.datetime(2120, 1, 1, tzinfo=datetime.timezone.utc)
_SUBJECT_CN       = "lapsecoin-relay-rendezvous"


def _seed_for(genesis_hash: str, addr: str) -> bytes:
    """32 bytes, used directly as an Ed25519 private key seed. Public and
    deterministic on purpose (see module docstring): this is a rendezvous
    label, not a secret, so there's no reason to keep it unpredictable."""
    return hashlib.sha256(f"{genesis_hash}|relay-identity|{addr}".encode()).digest()


def _cert_der_for(genesis_hash: str, addr: str) -> bytes:
    """The exact DER bytes of the self-signed certificate addr's node
    would present when joining a relay for this genesis. Deterministic:
    same inputs always produce byte-identical output, on any machine,
    which is the whole mechanism (see module docstring)."""
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        _seed_for(genesis_hash, addr))
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, _SUBJECT_CN),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(_FIXED_SERIAL)
        .not_valid_before(_FIXED_NOT_BEFORE)
        .not_valid_after(_FIXED_NOT_AFTER)
        .sign(private_key, None)  # None: Ed25519 has no separate hash alg
    )
    return cert.public_bytes(serialization.Encoding.DER)


def device_id_for(genesis_hash: str, addr: str) -> bytes:
    """The 32-byte device ID a relay would derive for addr's node, i.e.
    SHA-256 of that deterministic certificate's DER bytes. Fits the
    protocol's own <=32-byte ID field (ConnectRequest.ID,
    JoinSessionRequest.Key, SessionInvitation.From) exactly.

    Callers use this both for their own outgoing JoinRelayRequest identity
    and, just as importantly, to predict a peer's ID from nothing but the
    peer's already-known addr, no message exchange required."""
    return hashlib.sha256(_cert_der_for(genesis_hash, addr)).digest()


def identity_for(genesis_hash: str, addr: str):
    """(private_key, certificate) for addr's node, to actually present
    over TLS when *we* are addr. Only ever called with our own addr, never
    a peer's (we have no reason to hold a peer's private key, and
    device_id_for above never needs one to predict a peer's ID)."""
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        _seed_for(genesis_hash, addr))
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, _SUBJECT_CN),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(_FIXED_SERIAL)
        .not_valid_before(_FIXED_NOT_BEFORE)
        .not_valid_after(_FIXED_NOT_AFTER)
        .sign(private_key, None)
    )
    return private_key, cert


def identity_pem_for(genesis_hash: str, addr: str):
    """(key_pem_bytes, cert_pem_bytes) for addr's node, PEM-encoded since
    that's what ssl.SSLContext.load_cert_chain needs on disk. Callers write
    these to a tempfile; nothing here touches the filesystem itself, so
    this stays independently testable."""
    private_key, cert = identity_for(genesis_hash, addr)
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem
