"""Antigravity desktop/standalone app quota RPC polling."""

import http.client
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

from ..store import stamp
from ..util import is_finite_number as number, log_exception
from .http import NoRedirect


# A 5h window resets at most five hours after any observation; allow for clock skew.
AGY_5H_HORIZON = 18000 + 600
# The same original reset jitters by seconds between status-line and app responses.
AGY_RESET_MATCH = 300
AGY_SPLIT_BUCKET = re.compile(r'(gemini|3p)-(5h|weekly)-\d+')


def agy_app_limits(store, c, data, checked):
    """Desktop-app GetUserStatus: per-model quotaInfo, stored in the status-line buckets.

    Each model carries one quotaInfo (the window that currently binds) and no window
    length. A reset more than five hours away can only be the weekly window. A nearer
    reset is the weekly one only when it matches the family's known weekly reset;
    otherwise it is the 5h window. Models of a family that report the same
    (fraction, reset) are one shared quota; differing values are stored separately
    with numerical suffixes, never merged. Nothing is written for a bucket the
    response did not describe."""
    c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket LIKE 'app-%'")
    # Bucket labels are derived from the bucket names in the UI.
    c.execute("DELETE FROM state WHERE key LIKE 'label:antigravity:%'")
    configs = (((data.get('userStatus') or {}).get('cascadeModelConfigData') or {}).get('clientModelConfigs')) or []
    weekly = {
        r[0]: r[1]
        for r in c.execute(
            "SELECT bucket,resets FROM limits WHERE route='antigravity' "
            "AND bucket IN ('gemini-weekly','3p-weekly') AND resets>?",
            (checked,),
        )
    }
    groups = {}
    skipped = 0
    for cfg in configs:
        if not isinstance(cfg, dict):
            continue
        quota = cfg.get('quotaInfo')
        if not isinstance(quota, dict):
            continue
        model = str(cfg.get('modelId') or '')
        family = 'gemini' if model.lower().startswith('gemini') else '3p'
        fraction = quota.get('remainingFraction')
        # QuotaInfo.remaining_fraction is a plain proto3 float without presence in
        # language server 2.19.1, so protojson omits exactly 0.0. Only a quota that
        # still carries its reset is read as exhausted.
        if fraction is None and quota.get('resetTime'):
            fraction = 0.0
        try:
            reset = stamp(quota.get('resetTime')) if quota.get('resetTime') else None
        except (TypeError, ValueError, AttributeError):
            reset = None
        if not number(fraction) or not 0 <= fraction <= 1 or not number(reset) or reset <= checked:
            skipped += 1
            continue
        known = weekly.get(family + '-weekly')
        window = (
            'weekly' if reset - checked > AGY_5H_HORIZON or (known and abs(reset - known) <= AGY_RESET_MATCH) else '5h'
        )
        groups.setdefault((family, window), {}).setdefault((fraction, reset), []).append(model)
    written = set()
    split = False
    for (family, window), values in sorted(groups.items()):
        split = split or len(values) > 1
        for n, ((fraction, reset), models) in enumerate(sorted(values.items(), key=lambda kv: (-len(kv[1]), kv[0][0]))):
            bucket = f'{family}-{window}' + ('' if n == 0 else f'-{n + 1}')
            store.limit(c, 'antigravity', bucket, fraction * 100, reset, checked, 'antigravity-app')
            written.add(bucket)
    # Numbered buckets only describe one response's split; drop those it no longer has.
    for (bucket,) in c.execute(
        "SELECT bucket FROM limits WHERE route='antigravity' AND source='antigravity-app'"
    ).fetchall():
        if AGY_SPLIT_BUCKET.fullmatch(bucket) and bucket not in written:
            c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket=?", (bucket,))
    return len(written), skipped, split


# Last endpoint that answered GetUserStatus, per language-server pid.
_agy_endpoints = {}
_LOOPBACK = {
    'tcp': {'0100007F': '127.0.0.1', '00000000': '127.0.0.1'},
    'tcp6': {'00000000000000000000000001000000': '::1', '00000000000000000000000000000000': '::1'},
}


def _listening_ports(pid):
    """Loopback-reachable LISTEN endpoints owned by pid, from /proc (no ss/netstat dependency)."""
    inodes = set()
    try:
        for fd in os.listdir(f'/proc/{pid}/fd'):
            try:
                target = os.readlink(f'/proc/{pid}/fd/{fd}')
            except OSError:
                continue
            if target.startswith('socket:['):
                inodes.add(target[8:-1])
    except OSError:
        return []
    endpoints = set()
    for table, hosts in _LOOPBACK.items():
        try:
            with open(f'/proc/{pid}/net/{table}') as f:
                for line in list(f)[1:]:
                    parts = line.split()
                    if len(parts) > 9 and parts[3] == '0A' and parts[9] in inodes:
                        address, port = parts[1].split(':')
                        if address in hosts:
                            endpoints.add((hosts[address], int(port, 16)))
        except OSError:
            continue
    return sorted(endpoints)


def agy_app_servers():
    """Same-user Antigravity language servers. The CSRF token stays in memory only."""
    if not os.path.isdir('/proc'):
        return []
    found = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f'/proc/{entry}').st_uid != os.getuid():
                continue
            with open(f'/proc/{entry}/cmdline', 'rb') as f:
                args = f.read().split(b'\0')
        except OSError:
            continue
        if not args or not args[0].endswith(b'/language_server') or b'antigravity' not in args[0]:
            continue
        token = None
        for i, arg in enumerate(args):
            if arg == b'--csrf_token' and i + 1 < len(args):
                token = args[i + 1].decode(errors='replace')
            elif arg.startswith(b'--csrf_token='):
                token = arg.split(b'=', 1)[1].decode(errors='replace')
        if token:
            found.append((int(entry), token))
    return found


def _tls_ready(host, port, context, timeout):
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as raw,
            context.wrap_socket(raw, server_hostname=host),
        ):
            return True
    except OSError:
        return False


def _agy_post(servers, method, timeout=10, probe=2):
    import ssl

    from .. import limits as _limits

    # Loopback only, to a port owned by the server process; its certificate is self-signed.
    context = ssl.create_default_context()
    # Do not inherit a weaker protocol floor from the host OpenSSL configuration.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    body = json.dumps({'metadata': {'ideName': 'antigravity', 'extensionName': 'antigravity', 'locale': 'en'}}).encode()
    for pid in set(_agy_endpoints) - {pid for pid, _ in servers}:
        del _agy_endpoints[pid]
    last = None
    for pid, token in servers:
        endpoints = _limits._listening_ports(pid)
        cached = _agy_endpoints.get(pid)
        if cached in endpoints:
            endpoints = [cached] + [e for e in endpoints if e != cached]
        for host, port in endpoints:
            # Other ports of the server (LSP, extension host) may accept and never speak TLS;
            # a short handshake probe keeps them from holding the poll for the full timeout.
            if (host, port) != cached and not _limits._tls_ready(host, port, context, probe):
                last = 'TLS 미응답'
                continue
            request = urllib.request.Request(
                f'https://{"[" + host + "]" if ":" in host else host}:{port}/exa.language_server_pb.LanguageServerService/{method}',
                data=body,
                headers={
                    'Content-Type': 'application/json',
                    'Connect-Protocol-Version': '1',
                    'X-Codeium-Csrf-Token': token,
                },
            )
            try:
                with urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), NoRedirect, urllib.request.HTTPSHandler(context=context)
                ).open(request, timeout=timeout) as response:
                    data = json.load(response)
                _agy_endpoints[pid] = (host, port)
                return data
            except urllib.error.HTTPError as exc:
                last = f'HTTP {exc.code}'
                exc.close()
            # A port that answers without speaking HTTP is one more wrong port, not the end of the search.
            except (OSError, ValueError, http.client.HTTPException) as exc:
                last = type(exc).__name__
    raise ConnectionError(last or 'no listening port')


def read_agy_app(servers, timeout=10, probe=2):
    return _agy_post(servers, 'GetUserStatus', timeout, probe)


def read_agy_quota(servers, timeout=10, probe=2):
    return _agy_post(servers, 'RetrieveUserQuotaSummary', timeout, probe)


# Dedicated named-pool buckets (e.g. 'gemini-argon-wtu-weekly') distinct from the base 4 buckets.
AGY_BASE_BUCKETS = frozenset({'gemini-5h', 'gemini-weekly', '3p-5h', '3p-weekly'})
AGY_POOL_BUCKET = re.compile(r'(gemini|3p)-[a-z0-9-]+-(5h|weekly)(?:-\d+)?')
AGY_QUOTA_BUCKET = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


def agy_quota_limits(store, c, data, checked):
    """Desktop-app RetrieveUserQuotaSummary: every bucket of every quota family.

    Each group carries all its windows with an explicit bucketId and window
    label, so no reset-horizon guessing is needed for these rows.
    remainingFraction is a plain proto3 float: an omitted value with a reset is
    a real 0 (exhausted). Buckets the response cannot describe are skipped."""
    c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket LIKE 'app-%'")
    c.execute("DELETE FROM state WHERE key LIKE 'label:antigravity:%'")
    payload = data.get('response')
    if not isinstance(payload, dict):
        payload = data
    groups = payload.get('groups') if isinstance(payload, dict) else None
    written = set()
    skipped = 0
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        for row in group.get('buckets') or []:
            if not isinstance(row, dict):
                skipped += 1
                continue
            bucket = row.get('bucketId')
            fraction = row.get('remainingFraction')
            if fraction is None and row.get('resetTime'):
                fraction = 0.0
            try:
                reset = stamp(row.get('resetTime')) if row.get('resetTime') else None
            except (TypeError, ValueError, AttributeError):
                reset = None
            if (
                not isinstance(bucket, str)
                or not AGY_QUOTA_BUCKET.fullmatch(bucket)
                or not number(fraction)
                or not 0 <= fraction <= 1
                or not number(reset)
                or reset <= checked
            ):
                skipped += 1
                continue
            store.limit(c, 'antigravity', bucket, fraction * 100, reset, checked, 'antigravity-app')
            written.add(bucket)
    if written:
        store.save_state(
            c,
            'quota_snapshot:antigravity',
            dict(checked=checked, source='antigravity-app', buckets=sorted(written)),
        )
    # Numbered splits exist only when per-model reads disagree; the group-level
    # summary never produces them, so drop leftovers of an earlier read. When the
    # summary parsed cleanly without skipped buckets, also drop retired dedicated
    # pool buckets (e.g. a removed model-specific pool like gemini-argon-wtu-weekly);
    # limit_history retains past observations for reports.
    clean_summary = bool(written) and skipped == 0
    for (bucket,) in c.execute(
        "SELECT bucket FROM limits WHERE route='antigravity' AND source='antigravity-app'"
    ).fetchall():
        if bucket not in written and (
            AGY_SPLIT_BUCKET.fullmatch(bucket) or (clean_summary and AGY_POOL_BUCKET.fullmatch(bucket))
        ):
            c.execute("DELETE FROM limits WHERE route='antigravity' AND bucket=?", (bucket,))
    return len(written), skipped


def poll_antigravity_app(store, settings, servers=None, reader=None, quota_reader=None):
    """Quota as seen by the running desktop app; distinct from status-line receipt.

    RetrieveUserQuotaSummary is tried first because it describes every window of
    every quota family. GetUserStatus — one binding window per model — remains
    the fallback for servers that cannot answer it. Never raises: a failure here
    must not stop the other providers' polling."""
    if settings.get('antigravity_app_quota') is False:
        return
    from .. import limits as _limits

    checked = time.time()
    try:
        servers = _limits.agy_app_servers() if servers is None else servers
        if not servers:
            with store.connect() as c:
                if c.execute("SELECT 1 FROM sources WHERE name='antigravity-app'").fetchone():
                    store.source(
                        c,
                        'antigravity-app',
                        'unavailable',
                        '데스크톱 앱 language server 미발견 · 구형 앱 실행 중 또는 Antigravity 2.34+는 상태줄 경로를 사용합니다.',
                        checked,
                    )
            return
        try:
            summary = (quota_reader or _limits.read_agy_quota)(servers)
        except ConnectionError:
            summary = None
        if summary is not None:
            with store.connect() as c:
                n, skipped = agy_quota_limits(store, c, summary, checked)
                if n:
                    notes = ['WSL 데스크톱 앱 한도 요약 조회 · 내부 경로 · 응답의 창 레이블 사용']
                    if skipped:
                        notes.append(f'잔여 비율·초기화 시각을 해석할 수 없는 버킷 {skipped}개(미반영)')
                    store.source(c, 'antigravity-app', 'partial' if skipped else 'ok', ' · '.join(notes), checked)
                    return
        data = (reader or _limits.read_agy_app)(servers)
        with store.connect() as c:
            n, skipped, split = agy_app_limits(store, c, data, checked)
            notes = ['WSL 데스크톱 앱 조회 · 내부 경로 · 창 길이는 초기화 시각으로 판단']
            if split:
                notes.append('같은 계열의 서로 다른 한도를 따로 표시')
            if skipped:
                notes.append(f'잔여 비율·초기화 시각을 해석할 수 없는 모델 {skipped}개(미반영)')
            store.source(
                c,
                'antigravity-app',
                'partial' if n and (split or skipped) else 'ok' if n else 'unavailable',
                ' · '.join(notes) if n else '앱 응답에 해석 가능한 한도 필드가 없습니다.',
                checked,
            )
    except Exception as exc:
        log_exception('antigravity-app poll', exc)
        # Neither the CSRF token nor the response body is recorded.
        with store.connect() as c:
            store.source(
                c,
                'antigravity-app',
                'error',
                f'데스크톱 앱 한도 조회 실패 ({exc if isinstance(exc, ConnectionError) else type(exc).__name__})',
                checked,
            )
