"""Non-destructive project alias validation and query-time resolution."""

MAX_ALIASES = 200
MAX_NAME_LEN = 200
UNCLASSIFIED = '미분류'


def _valid_name(value):
    return (
        isinstance(value, str)
        and 0 < len(value.strip()) <= MAX_NAME_LEN
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    )


def validate_aliases(raw):
    """Validate a {source_project: canonical_project} mapping.

    Rejects self-mappings, chains (a -> b -> c), and cycles so canonical names
    are unambiguous and reversible without mutating SQLite history.
    """
    if not isinstance(raw, dict) or len(raw) > MAX_ALIASES:
        raise ValueError('프로젝트 별칭 목록을 확인하세요.')
    clean = {}
    for src, dst in raw.items():
        if not _valid_name(src) or not _valid_name(dst):
            raise ValueError('프로젝트 이름은 1~200자의 일반 문자열이어야 합니다.')
        s, d = src.strip(), dst.strip()
        if s == UNCLASSIFIED:
            raise ValueError('미분류 프로젝트는 별칭 원본으로 지정할 수 없습니다.')
        if s == d:
            raise ValueError('같은 이름으로 별칭을 지정할 수 없습니다.')
        clean[s] = d
    for src, dst in clean.items():
        if dst in clean:
            raise ValueError('연쇄 또는 순환 별칭은 지원하지 않습니다. 최종 대상 이름으로 직접 연결하세요.')
    return clean


def safe_aliases(raw):
    """Best-effort clean dict from stored config (never raises on corrupted disk data)."""
    if not isinstance(raw, dict):
        return {}
    try:
        return validate_aliases(raw)
    except ValueError:
        return {}


def resolve_project(name, aliases):
    """Map a raw project label (or None/'') to its canonical display name."""
    label = name or UNCLASSIFIED
    if not aliases:
        return label
    return aliases.get(label, label)


def raw_names_for(canonical, aliases):
    """Return every raw project label that resolves to `canonical`."""
    if not aliases:
        return [canonical]
    sources = sorted(src for src, dst in aliases.items() if dst == canonical)
    if canonical not in aliases:
        return [canonical, *sources]
    return sources
