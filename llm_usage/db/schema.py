"""Store schema constants and pure time/path/SQL helpers."""

from datetime import datetime, timedelta
import functools
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from zoneinfo import ZoneInfo

from ..claude_accounts import is_claude

KST = ZoneInfo('Asia/Seoul')
TOKENS = ('uncached_input', 'cached_input', 'output', 'cache_creation', 'reasoning')
DETAILS = (
    'cache_creation_5m',
    'cache_creation_1h',
    'cache_creation_unknown',
    'cache_ttl_observed',
    'cache_ttl_conflict',
    'reasoning_observed',
    'advisor_calls',
)
METRICS = (*TOKENS, *DETAILS)
SERIES_KEYS = (*METRICS, 'requests', 'cost', 'unpriced')
HISTORY_FIELDS = ('checked', 'remaining', 'resets', 'break_before', 'break_reason')
# Sources fed by a terminal status line rather than by polling.
STATUS_LINE_SOURCES = {'claude-code', 'antigravity', 'antigravity-tokens'}
# A month's end is extrapolated from its days so far only from this day of the month on:
# a few days scaled to a whole month are noise. Before it there is no estimate at all.
PROJECTION_FROM_DAY = 7
ONE_TIME_SOURCES = {'session-backfill'}
# Reasoning effort as the tools record it. Per-token prices do not depend on it; what it
# changes is how many tokens a request uses, so it is kept as a dimension of the usage.
EFFORT_LEVELS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
_EFFORT_SUFFIX = re.compile(r'(?: \((%s)\)|-(%s))$' % ('|'.join(EFFORT_LEVELS[1:]), '|'.join(EFFORT_LEVELS[1:])))


def clean_label(value):
    """A short lower-case word (an effort level, a speed tier) or None."""
    return value.lower() if isinstance(value, str) and re.fullmatch(r'[A-Za-z]{2,12}', value) else None


# Only these routes name the effort in the model. Elsewhere such an ending is part of the
# model's own name ('gpt-5.1-codex-max'), so it is neither read as an effort nor cut off.
NAMED_EFFORT_ROUTES = ('devin', 'antigravity')


def model_effort(model):
    """The effort a model name carries ('swe-2 (max)', 'claude-sonnet-5-5-high'), if any."""
    found = _EFFORT_SUFFIX.search(model or '')
    return (found.group(1) or found.group(2)) if found else None


# Source detail written while a subscription is marked ended in the settings.
USER_ENDED_DETAIL = '설정에서 구독 종료로 표시 · 한도 조회 중지'
SUPPORTED_ROUTES = ('codex', 'claude-code', 'antigravity', 'opencode-go', 'devin')
# (route, shorter-window bucket) -> the longer window that also gates usage.
QUOTA_PARENTS = {
    ('claude-code', 'five_hour'): 'seven_day',
    ('devin', 'daily'): 'weekly',
    ('antigravity', 'gemini-5h'): 'gemini-weekly',
    ('antigravity', '3p-5h'): '3p-weekly',
}
BUCKET_SQL = {
    'hour': 'CAST((ts+32400)/3600 AS INTEGER)*3600-32400',
    'day': 'CAST((ts+32400)/86400 AS INTEGER)*86400-32400',
    'week': "CAST((ts+32400)/86400 AS INTEGER)*86400-32400-((CAST(strftime('%w',ts,'unixepoch','+9 hours') AS INTEGER)+6)%7)*86400",
    'month': "CAST(strftime('%s',ts,'unixepoch','+9 hours','start of month') AS INTEGER)-32400",
}


def route_events(route):
    """SQL filter for the usage events of a quota route. Claude accounts share route
    'claude-code'; pending (unresolved) events count for the original account."""
    if route == 'claude-code':
        return "route='claude-code' AND (account IS NULL OR account='claude-code')", ()
    if is_claude(route):
        return "route='claude-code' AND account=?", (route,)
    return 'route=?', (route,)


def calendar_buckets(start, end, granularity):
    if end <= start:
        raise ValueError('시작·종료일이 올바르지 않습니다.')
    cursor = start.replace(minute=0, second=0, microsecond=0)
    if granularity != 'hour':
        cursor = cursor.replace(hour=0)
    if granularity == 'week':
        cursor -= timedelta(days=cursor.weekday())
    elif granularity == 'month':
        cursor = cursor.replace(day=1)
    result = []
    while cursor < end:
        if len(result) >= 50000:
            raise ValueError('기간이 너무 깁니다.')
        if granularity == 'month':
            following = (
                cursor.replace(year=cursor.year + 1, month=1)
                if cursor.month == 12
                else cursor.replace(month=cursor.month + 1)
            )
        else:
            following = cursor + timedelta(seconds={'hour': 3600, 'day': 86400, 'week': 604800}[granularity])
        result.append((cursor, following))
        cursor = following
    return result


def stamp(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


GENERIC_FOLDERS = ('', 'projects', 'home', 'mnt', '~')


@functools.lru_cache(maxsize=4096)
def repository_root(cwd):
    """Main repository folder for a path inside a git checkout or linked worktree.

    A linked worktree's .git file points at <main>/.git/worktrees/<name>, so its
    work is attributed to <main>. A Claude Code agent worktree that has already
    been removed is recognised by its <repo>/.claude/worktrees/<name> path."""
    text = str(cwd).rstrip('/')
    if '/.claude/worktrees/' in text:
        return text.split('/.claude/worktrees/', 1)[0]
    # Only absolute POSIX paths are looked up; others (E:\\x) would resolve against our cwd.
    if not text.startswith('/'):
        return None
    path = Path(text)
    try:
        for folder in (path, *path.parents):
            marker = folder / '.git'
            if marker.is_dir() and (marker / 'HEAD').is_file():
                return str(folder)
            if marker.is_file():
                line = marker.read_text(errors='replace').strip()
                gitdir = Path(line[7:].strip()) if line.startswith('gitdir:') else None
                if gitdir and not gitdir.is_absolute():
                    gitdir = folder / gitdir
                if gitdir and gitdir.parent.name == 'worktrees' and gitdir.parent.parent.name == '.git':
                    return str(gitdir.parent.parent.parent)
                return str(folder)
    except OSError:
        pass
    return None


def project_label(cwd):
    """Store a basename only; existing database labels are deliberately not migrated."""
    if not cwd:
        return None
    text = str(cwd)
    windows = '\\' in text or bool(re.match(r'^[A-Za-z]:', text)) or text.startswith('//')
    if windows:
        path = PureWindowsPath(text)
        # Historical agent worktree paths can be grouped without touching a Windows disk.
        normalized = path.as_posix()
        if '/.claude/worktrees/' in normalized:
            path = PureWindowsPath(normalized.split('/.claude/worktrees/', 1)[0])
        name = path.name
    else:
        root = repository_root(text)
        name = PurePosixPath(root or text).name
        if root and name in GENERIC_FOLDERS:
            name = PurePosixPath(text).name
    return name if name not in ('', '.', '..', '~') else None
