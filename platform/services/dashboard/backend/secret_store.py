"""Fernet-based at-rest encryption facade.

The ONLY module that imports cryptography.fernet directly. All other code
goes through the two functions exported here.
"""

from cryptography.fernet import Fernet
from settings import settings

_fernet = Fernet(settings.dashboard_secret_key.encode())


def encrypt(plaintext: str) -> bytes:
    """Encrypt a UTF-8 string. Returns the Fernet token as bytes."""
    return _fernet.encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    """Decrypt a Fernet token. Raises cryptography.fernet.InvalidToken on
    tamper, wrong key, or malformed input."""
    return _fernet.decrypt(token).decode("utf-8")
