import sys
import os
import hashlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import relay_identity as ri  # noqa: E402

GENESIS = "deadbeef" * 8
ADDR_A  = "10.0.0.1:9000"
ADDR_B  = "10.0.0.2:9001"


class TestDeviceIdDeterminism:
    def test_same_inputs_produce_same_id(self):
        assert ri.device_id_for(GENESIS, ADDR_A) == ri.device_id_for(GENESIS, ADDR_A)

    def test_different_addr_produces_different_id(self):
        assert ri.device_id_for(GENESIS, ADDR_A) != ri.device_id_for(GENESIS, ADDR_B)

    def test_different_genesis_produces_different_id(self):
        assert ri.device_id_for(GENESIS, ADDR_A) != ri.device_id_for("other-genesis", ADDR_A)

    def test_id_is_32_bytes(self):
        assert len(ri.device_id_for(GENESIS, ADDR_A)) == 32


class TestCertMatchesDeviceId:
    def test_device_id_is_sha256_of_cert_der(self):
        """The whole scheme depends on this equality: a peer predicting
        our device ID from our addr alone must get the same 32 bytes our
        own presented certificate would hash to."""
        from cryptography.hazmat.primitives import serialization
        _, cert = ri.identity_for(GENESIS, ADDR_A)
        der = cert.public_bytes(serialization.Encoding.DER)
        assert hashlib.sha256(der).digest() == ri.device_id_for(GENESIS, ADDR_A)

    def test_identity_for_is_itself_deterministic(self):
        _, cert1 = ri.identity_for(GENESIS, ADDR_A)
        _, cert2 = ri.identity_for(GENESIS, ADDR_A)
        from cryptography.hazmat.primitives import serialization
        assert (cert1.public_bytes(serialization.Encoding.DER)
                == cert2.public_bytes(serialization.Encoding.DER))


class TestIdentityPem:
    def test_pem_roundtrip_shape(self):
        key_pem, cert_pem = ri.identity_pem_for(GENESIS, ADDR_A)
        assert key_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")

    def test_pem_is_loadable_by_the_ssl_module(self):
        """Not just well-formed PEM: actually usable by Python's ssl
        module the way relay_client would use it, since that's the one
        thing that matters for this to be useful at all."""
        import ssl
        import tempfile
        key_pem, cert_pem = ri.identity_pem_for(GENESIS, ADDR_A)
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
            kf.write(key_pem)
            key_path = kf.name
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cf:
            cf.write(cert_pem)
            cert_path = cf.name
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        finally:
            os.unlink(key_path)
            os.unlink(cert_path)
