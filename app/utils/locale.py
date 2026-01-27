import re

LOCALE_RE = re.compile(r"^[a-z]{2}(-[a-z]{2,4})?$", re.IGNORECASE)


def normalize_locale(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "-" in value:
        language, region = value.split("-", 1)
        normalized = f"{language.lower()}-{region.lower()}"
    else:
        normalized = value.lower()
    return normalized


def is_valid_locale(value: str) -> bool:
    return bool(LOCALE_RE.match(value))
