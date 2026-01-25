import os
from cryptography.fernet import Fernet


def get_fernet() -> Fernet:
    key = os.getenv("FERNET_KEY")
    if not key:
        raise RuntimeError("FERNET_KEY is required")
    return Fernet(key.encode())


def encrypt_token(token: str) -> str:
    fernet = get_fernet()
    return fernet.encrypt(token.encode()).decode()


def decrypt_token(token_encrypted: str) -> str:
    fernet = get_fernet()
    return fernet.decrypt(token_encrypted.encode()).decode()
