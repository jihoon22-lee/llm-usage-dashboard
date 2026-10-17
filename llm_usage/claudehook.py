"""Standalone Claude Code hook: which account handles a session turn. Stdlib only.

Claude Code runs it for SessionStart, UserPromptSubmit and Stop with the hook
event on stdin and the CLAUDE_CONFIG_DIR of the account it runs under. It records
the session ID, the time, the hashed account identity of that config home and the
home path. No prompt, transcript text or credential leaves stdin or the home.

It prints nothing (SessionStart/UserPromptSubmit output would enter the model's
context) and always exits 0, so a failure here can never block or change a turn.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

EVENTS = ('SessionStart', 'UserPromptSubmit', 'Stop')
SESSION = re.compile(r'^[A-Za-z0-9._-]{1,100}$')


def account_key(identity):
    """Equal to llm_usage.resources.key('claude-code', identity)."""
    return hashlib.sha256(json.dumps(['claude-code', list(identity)], sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def config_home(environ=None):
    """The config home and native account file Claude Code itself uses."""
    environ = os.environ if environ is None else environ
    custom = environ.get('CLAUDE_CONFIG_DIR')
    if custom:
        home = Path(custom).expanduser()
        return home, home / '.claude.json'
    return Path.home() / '.claude', Path.home() / '.claude.json'


def native_account(account_file):
    """Hashed (accountUuid, organizationUuid) of the signed-in account, or None."""
    try:
        data = json.loads(Path(account_file).read_text()).get('oauthAccount') or {}
        user, org = data.get('accountUuid'), data.get('organizationUuid')
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if all(isinstance(v, str) and re.fullmatch('[A-Za-z0-9-]{1,100}', v) for v in (user, org)):
        return account_key((user, org))
    return None


def record(directory, event, session, now=None, environ=None):
    if event not in EVENTS or not isinstance(session, str) or not SESSION.match(session):
        return False
    home, account_file = config_home(environ)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'claude-accounts.db'
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600); os.close(fd)
    now = time.time() if now is None else now
    with closing(sqlite3.connect(path, timeout=1)) as c, c:
        c.execute('CREATE TABLE IF NOT EXISTS markers (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, '
                  'session TEXT NOT NULL, event TEXT NOT NULL, account TEXT, home TEXT NOT NULL)')
        c.execute('INSERT INTO markers (ts,session,event,account,home) VALUES (?,?,?,?,?)',
                  (now, session, event, native_account(account_file), str(home)[:300]))
        # The collector keeps its own copy; the inbox only bridges a few days.
        if int(now) % 50 == 0:
            c.execute('DELETE FROM markers WHERE ts<?', (now - 14 * 86400,))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    try:
        data = json.loads(sys.stdin.buffer.read() or b'{}')
        if isinstance(data, dict):
            record(args.directory, data.get('hook_event_name'), data.get('session_id'))
    except Exception:
        pass  # Never alter or block the coding tool.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
