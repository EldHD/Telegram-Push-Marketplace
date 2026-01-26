import base64
import binascii
import logging
import os
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)
_FERNET: Fernet | None = None


def _is_valid_fernet_key(value: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(value.encode())
    except (ValueError, binascii.Error):
        return False
    return len(decoded) == 32


def get_fernet() -> Fernet:
    global _FERNET
    if _FERNET:
        return _FERNET
    env = os.getenv("ENV", "development").lower()
    key = os.getenv("FERNET_KEY")
    if key and _is_valid_fernet_key(key):
        _FERNET = Fernet(key.encode())
        return _FERNET
    if env == "production":
        raise RuntimeError("FERNET_KEY is required and must be a valid base64 key in production")
    temp_key = Fernet.generate_key()
    logger.warning("FERNET_KEY missing or invalid; using temporary key for development.")
    _FERNET = Fernet(temp_key)
    return _FERNET


def encrypt_token(token: str) -> str:
    fernet = get_fernet()
    return fernet.encrypt(token.encode()).decode()


def decrypt_token(token_encrypted: str) -> str:
    fernet = get_fernet()
    return fernet.decrypt(token_encrypted.encode()).decode()
