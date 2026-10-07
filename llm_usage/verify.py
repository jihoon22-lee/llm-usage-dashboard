"""Read-only reconciliation of events against retained original observations.

The collector keeps raw cumulative points (codex_points), per-response protobuf
metadata (antigravity), and Devin's own sessions.db checkpoints, so this module
re-derives expected values without calling any collector/parser code and
compares them to the events table. Used by tests/verify_record_totals.py and by
the collector's daily self-check.
"""
import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .devin import checkpoint_of as _checkpoint_of, model_name as _model_name
from .sqlite_ro import open_readonly
from .store import identity as _identity


def reconcile(path, cfg):
    path = Path(path)
    return dict(codex=_codex(path), antigravity=_antigravity(path, cfg), devin=_devin(path, cfg))


def failed(result):
    codex, agy, dev = result['codex'], result['antigravity'], result['devin']
    return bool(codex['mismatches'] or dev['missing_events'] or dev['mismatched_events'] or dev['source_errors']
        or any(agy[k] for k in ('source_errors', 'source_conflicts', 'output_split_errors',
                                'missing_events', 'mismatched_events', 'alias_errors')))


def _codex(path):
    counts = dict(matched_complete_monotonic_sessions=0, matched_counter_reset_sessions=0,
                  matched_partial_baseline_sessions=0, ambiguous_timestamp_sessions=0,
                  inconsistent_component_sessions=0, zero_request_baseline_sessions=0)
    failures = []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row; c.execute('BEGIN')
        sessions = [r[0] for r in c.execute('SELECT DISTINCT session FROM codex_points')]
        for session in sessions:
            rows = c.execute('SELECT * FROM codex_points WHERE session=? ORDER BY ts,input,output,id', (session,)).fetchall()
            columns = ('input', 'cached', 'output', 'creation', 'reasoning')
            observations = [(row['ts'], tuple(row[k] for k in columns), tuple(row['last_'+k] for k in columns)) for row in rows]
            if any(a[0] == b[0] and a[1] != b[1] for a, b in zip(observations, observations[1:])):
                counts['ambiguous_timestamp_sessions'] += 1; continue
            previous = None; expected = [0]*5; reset = False; partial = observations[0][1] != observations[0][2]; invalid = False
            baseline = False; has_baseline = False
            for _, current, last in observations:
                if current == previous: continue
                restarted = previous is not None and (current[0] < previous[0] or current[2] < previous[2])
                reset = reset or restarted
                delta = last if previous is None or restarted else tuple(max(0, a-b) for a, b in zip(current, previous))
                if not any(last):
                    delta = last; baseline = True; has_baseline = True
                else:
                    if baseline and delta != last: delta = last
                    baseline = False
                if delta[0] < delta[1]+delta[3] or delta[2] < delta[4]: invalid = True
                expected = [a+b for a, b in zip(expected, delta)]; previous = current
            if invalid:
                counts['inconsistent_component_sessions'] += 1; continue
            if has_baseline: counts['zero_request_baseline_sessions'] += 1
            actual = tuple(c.execute('''SELECT COALESCE(SUM(uncached_input+cached_input+cache_creation),0),
              COALESCE(SUM(cached_input),0),COALESCE(SUM(output),0),COALESCE(SUM(cache_creation),0),COALESCE(SUM(reasoning),0)
              FROM events WHERE id IN (SELECT id FROM codex_points WHERE session=?)''', (session,)).fetchone())
            if actual != tuple(expected):
                failures.append({'session': session[:8], 'actual': actual, 'expected': tuple(expected)})
            else:
                counts['matched_counter_reset_sessions' if reset else 'matched_partial_baseline_sessions' if partial
                       else 'matched_complete_monotonic_sessions'] += 1
    return {**counts, 'mismatches': len(failures), 'mismatch_samples': failures[:10]}


def _decode(raw):
    offset = 0; result = {}
    def number():
        nonlocal offset
        n = 0; shift = 0
        while offset < len(raw) and shift < 70:
            b = raw[offset]; offset += 1; n |= (b & 127) << shift
            if b < 128: return n
            shift += 7
        raise ValueError('invalid protobuf integer')
    while offset < len(raw):
        tag = number(); field, wire = divmod(tag, 8)
        if not field: raise ValueError('invalid protobuf tag')
        if wire == 0: value = number()
        elif wire in (1, 2, 5):
            size = number() if wire == 2 else 8 if wire == 1 else 4
            if offset+size > len(raw): raise ValueError('invalid protobuf field')
            value = raw[offset:offset+size]; offset += size
        else: raise ValueError('unsupported protobuf field')
        result[field] = value
    return result


def _alias(api, kind, value):
    return hashlib.sha256(json.dumps(['antigravity', api, kind, value.hex()], sort_keys=True).encode()).hexdigest()


def _antigravity(path, settings):
    paths = set()
    for home in settings.get('homes', []):
        for relative in ('.gemini/antigravity-cli/conversations', '.gemini/antigravity/conversations'):
            paths.update((Path(home)/relative).glob('*.db'))
    for source in settings.get('sources', []):
        if source.get('kind') == 'antigravity':
            root = Path(source['path']); paths.update(root.glob('*.db') if root.is_dir() else [root])
    original = {}; errors = 0; duplicates = 0; conflicts = 0; split_errors = 0
    for source in sorted(paths):
        try:
            with closing(open_readonly(source)) as c:
                c.execute('BEGIN')
                for raw, in c.execute('SELECT metadata FROM steps WHERE metadata IS NOT NULL'):
                    try:
                        metadata = _decode(raw); usage = _decode(metadata.get(9, b''))
                        if not usage: continue
                        ids = [_alias(usage.get(6, 0), kind, usage[k]) for k, kind in ((11, 'response'), (12, 'provider-message'), (7, 'message')) if usage.get(k)]
                        if not ids: raise ValueError('missing identity')
                        stamp = _decode(metadata.get(1) or metadata.get(32) or b'')
                        if not stamp.get(1): raise ValueError('missing timestamp')
                        timestamp = stamp[1]+stamp.get(2, 0)/10**9
                        values = tuple(usage.get(k, 0) for k in (2, 5, 3, 4, 9))
                        if 10 in usage and values[2] != values[4]+usage[10]: split_errors += 1
                        if ids[0] in original:
                            duplicates += 1
                            if original[ids[0]][1] != values: conflicts += 1
                        else: original[ids[0]] = (ids, values, timestamp)
                    except (ValueError, TypeError): errors += 1
        except (OSError, sqlite3.Error): errors += 1
    missing = wrong = alias_errors = 0; used = set()
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('BEGIN')
        for ids, values, timestamp in original.values():
            keys = {r[0] for r in c.execute('SELECT DISTINCT request_id FROM antigravity_request_aliases WHERE alias IN ('
                                            + ','.join('?' for _ in ids) + ')', ids)}
            if not keys: missing += 1; continue
            if len(keys) != 1: alias_errors += 1; continue
            key = next(iter(keys))
            if key in used: alias_errors += 1
            used.add(key)
            actual = c.execute("SELECT uncached_input,cached_input,output,cache_creation,reasoning,ts FROM events WHERE id=? AND route='antigravity'", (key,)).fetchone()
            if actual is None: missing += 1
            elif tuple(actual[:5]) != values or abs(actual[5]-timestamp) > 1e-6: wrong += 1
        retained = {r[0] for r in c.execute("SELECT id FROM events WHERE route='antigravity'")}
    return dict(source_dbs=len(paths), original_requests=len(original), duplicate_source_rows=duplicates,
                source_errors=errors, source_conflicts=conflicts, output_split_errors=split_errors,
                missing_events=missing, mismatched_events=wrong, alias_errors=alias_errors,
                retained_without_current_source=len(retained-used), matched_requests=len(original)-missing-wrong-alias_errors,
                input=sum(v[1][0] for v in original.values()), cached=sum(v[1][1] for v in original.values()),
                output=sum(v[1][2] for v in original.values()), cache_creation=sum(v[1][3] for v in original.values()),
                reasoning=sum(v[1][4] for v in original.values()))


def _devin_db(path):
    src = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
    try:
        src.execute('SELECT 1 FROM sqlite_master').fetchone(); return src, None
    except sqlite3.Error:
        src.close()
    tmp = Path(tempfile.mkdtemp(prefix='verify-devin-')); dst = tmp/'sessions.db'
    shutil.copyfile(path, dst)
    try: shutil.copyfile(str(path)+'-wal', str(dst)+'-wal')
    except OSError: pass
    return sqlite3.connect(dst), tmp


def _devin_expected(cfg, checkpoints):
    found = {Path(s['path']) for s in cfg.get('sources', []) if s.get('kind') == 'devin'}
    for home in cfg.get('homes', []):
        for rel in ('.local/share/devin/cli/sessions.db', 'AppData/Roaming/devin/cli/sessions.db'):
            p = Path(home)/rel
            if p.exists(): found.add(p)
    expected = {}
    for dbfile in found:
        if not dbfile.exists(): continue
        # Only rows the collector already had a chance to scan are expected; rows
        # past its row_id checkpoint are collection lag, not missing events.
        checkpoint = _checkpoint_of(checkpoints.get('devin:'+str(dbfile), 0))['row_id']
        src, tmp = _devin_db(dbfile)
        try:
            for rid, row_id, raw in src.execute("SELECT json_extract(chat_message,'$.metadata.request_id'),row_id,chat_message "
                                                "FROM message_nodes WHERE json_extract(chat_message,'$.role')='assistant'"):
                if not rid or row_id > checkpoint: continue
                m = (json.loads(raw).get('metadata') or {}); t = m.get('metrics')
                if not isinstance(t, dict): continue
                vals = (int(t.get('input_tokens') or 0), int(t.get('cache_read_tokens') or 0),
                        int(t.get('output_tokens') or 0), int(t.get('cache_creation_tokens') or 0))
                # The collector skips snapshots whose metrics carry no tokens.
                if not any(vals): continue
                expected.setdefault(rid, (_model_name(m.get('generation_model')) or 'unknown', *vals))
        finally:
            src.close()
            if tmp: shutil.rmtree(tmp, ignore_errors=True)
    return expected


def _devin(path, cfg):
    result = dict(source_errors=0, missing_events=0, mismatched_events=0, requests=0)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as c:
            checkpoints = {k: json.loads(v) for k, v in c.execute("SELECT key,data FROM state WHERE key LIKE 'devin:%sessions.db'")}
            expected = _devin_expected(cfg, checkpoints); result['requests'] = len(expected)
            for rid, want in expected.items():
                row = c.execute('SELECT model,uncached_input,cached_input,output,cache_creation FROM events WHERE id=?',
                                (_identity('devin', rid),)).fetchone()
                if row is None: result['missing_events'] += 1
                elif tuple(row[1:]) != want[1:] or row[0] != want[0]: result['mismatched_events'] += 1
    except (OSError, sqlite3.Error): result['source_errors'] += 1
    return result
