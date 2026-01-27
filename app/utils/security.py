import base64
import binascii
import logging
import os
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)
_FERNET: Fernet | None = None


def validate_fernet_key(value: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(value.encode())
    except (ValueError, binascii.Error):
        return False
    return len(decoded) == 32


def ensure_fernet_key_config() -> None:
    env = os.getenv("ENV", "development").lower()
    debug = os.getenv("DEBUG", "false").lower() == "true"
    key = os.getenv("FERNET_KEY", "")
    if env == "production" and not debug and not validate_fernet_key(key):
        raise RuntimeError(
            "FERNET_KEY is required in production and must be 32 url-safe base64-encoded bytes."
        )


def get_fernet() -> Fernet:
    global _FERNET
    if _FERNET:
        return _FERNET
    env = os.getenv("ENV", "development").lower()
    key = os.getenv("FERNET_KEY")
    if key and validate_fernet_key(key):
        _FERNET = Fernet(key.encode())
        return _FERNET
    if env == "production":
        raise RuntimeError("FERNET_KEY is required and must be a valid base64 key in production")
    temp_key = Fernet.generate_key()
    logger.warning(
        "FERNET_KEY missing or invalid; using temporary key for development. "
        "Generate one with: python - <<'PY'\nfrom cryptography.fernet import Fernet\n"
        "print(Fernet.generate_key().decode())\nPY"
    )
    _FERNET = Fernet(temp_key)
    return _FERNET


def encrypt_token(token: str) -> str:
    fernet = get_fernet()
    return fernet.encrypt(token.encode()).decode()


def decrypt_token(token_encrypted: str) -> str:
    fernet = get_fernet()
    return fernet.decrypt(token_encrypted.encode()).decode()
