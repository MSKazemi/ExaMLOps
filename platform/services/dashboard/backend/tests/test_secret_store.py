"""Round-trip + tamper detection on the Fernet secret store."""

import pytest
from cryptography.fernet import InvalidToken


def test_round_trip_returns_original_plaintext():
    from secret_store import decrypt, encrypt

    token = encrypt("hello world")
    assert isinstance(token, bytes)
    assert decrypt(token) == "hello world"


def test_unicode_round_trip():
    from secret_store import decrypt, encrypt

    s = "naïve · MLOps · κλειδί"
    assert decrypt(encrypt(s)) == s


def test_tampered_token_raises():
    from secret_store import decrypt, encrypt

    token = encrypt("secret")
    tampered = token[:-1] + bytes([token[-1] ^ 0x01])
    with pytest.raises(InvalidToken):
        decrypt(tampered)


def test_empty_string_round_trip():
    from secret_store import decrypt, encrypt

    assert decrypt(encrypt("")) == ""
