import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.main import TOKEN_RE, _normalize_username
from app.utils.security import validate_fernet_key


def test_token_regex_accepts_botfather_format():
    token = "8279371393:AAFo-o1To78bVvKfpf7h5MkWT_S7q_Ngoe8"
    assert TOKEN_RE.match(token)


def test_normalize_username():
    assert _normalize_username("@MyBot") == "mybot"


def test_validate_fernet_key_rejects_invalid():
    assert validate_fernet_key("invalid") is False
