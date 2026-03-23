# tests/test_signing.py
from identity import sign_bytes, verify_bytes

def test_sign_and_verify_roundtrip():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    pk_hex = sk.verify_key.encode().hex()
    data = b"knowledge-pack-sha256-hash"
    sig = sign_bytes(sk, data)
    assert isinstance(sig, bytes)
    assert len(sig) == 64
    verify_bytes(pk_hex, data, sig)

def test_verify_bad_signature():
    from nacl.signing import SigningKey
    import pytest
    sk = SigningKey.generate()
    pk_hex = sk.verify_key.encode().hex()
    data = b"original"
    sig = sign_bytes(sk, data)
    with pytest.raises(Exception):
        verify_bytes(pk_hex, b"tampered", sig)

def test_verify_wrong_key():
    from nacl.signing import SigningKey
    import pytest
    sk1 = SigningKey.generate()
    sk2 = SigningKey.generate()
    data = b"test"
    sig = sign_bytes(sk1, data)
    with pytest.raises(Exception):
        verify_bytes(sk2.verify_key.encode().hex(), data, sig)
