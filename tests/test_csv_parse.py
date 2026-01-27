import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/telegram_push_test.db")

from app.main import parse_audience_rows


def test_parse_csv_with_semicolon_and_no_header():
    content = b"123;ru\n456;en\n"
    accepted, errors = parse_audience_rows(content)
    assert len(accepted) == 2
    assert errors == []


def test_parse_csv_with_header_and_bom():
    content = "\ufefftg_id,locale\n789,zh-hans".encode("utf-8")
    accepted, errors = parse_audience_rows(content)
    assert accepted == [(789, "zh-hans")]
    assert errors == []
