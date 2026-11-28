"""HTTP response compression (zstd preferred, gzip fallback) and weak ETag support."""

import gzip
import hashlib

try:
    from compression import zstd as _zstd
except ImportError:
    from backports import zstd as _zstd

MIN_COMPRESS_BYTES = 1024
COMPRESSIBLE_TYPES = frozenset(
    {
        'application/json',
        'application/javascript',
        'application/manifest+json',
        'image/svg+xml',
        'text/css',
        'text/html',
        'text/javascript',
        'text/plain',
    }
)


def parse_accept_encoding(header):
    """Return {encoding: q_value} for an Accept-Encoding header."""
    prefs = {}
    if not header:
        return prefs
    for raw in header.split(','):
        parts = [p.strip() for p in raw.split(';') if p.strip()]
        if not parts:
            continue
        name = parts[0].lower()
        q = 1.0
        for param in parts[1:]:
            if param.lower().startswith('q='):
                try:
                    q = float(param[2:].strip())
                except ValueError:
                    q = 0.0
        prefs[name] = max(0.0, min(1.0, q))
    return prefs


def choose_encoding(header):
    """Choose 'zstd' or 'gzip' according to Accept-Encoding, preferring zstd on ties."""
    prefs = parse_accept_encoding(header)
    if not prefs:
        return None
    star = prefs.get('*', 0.0)
    zstd_q = prefs.get('zstd', star if 'zstd' not in prefs else 0.0)
    gzip_q = prefs.get('gzip', star if 'gzip' not in prefs else 0.0)
    # Only pick zstd when explicitly advertised (or via * with positive q).
    candidates = []
    if zstd_q > 0 and ('zstd' in prefs or '*' in prefs):
        candidates.append((zstd_q, 2, 'zstd'))
    if gzip_q > 0 and ('gzip' in prefs or '*' in prefs):
        candidates.append((gzip_q, 1, 'gzip'))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def compress_bytes(data, encoding):
    if encoding == 'zstd':
        return _zstd.compress(data, level=3)
    if encoding == 'gzip':
        return gzip.compress(data, compresslevel=6)
    raise ValueError(f'unsupported encoding: {encoding}')


def _append_vary(response, value):
    existing = [v.strip() for v in (response.headers.get('Vary') or '').split(',') if v.strip()]
    if not any(v.lower() == value.lower() for v in existing):
        existing.append(value)
        response.headers['Vary'] = ', '.join(existing)


def apply_etag_and_compression(request, response, *, cacheable=False):
    """Apply weak ETag/304 (for cacheable static routes) and zstd/gzip compression."""
    if not (200 <= response.status_code < 300):
        return response
    mimetype = (response.mimetype or '').lower()
    compressible = mimetype in COMPRESSIBLE_TYPES or mimetype.startswith('text/')
    if not compressible:
        return response
    if response.direct_passthrough:
        response.direct_passthrough = False
        data = response.get_data()
        response.close()
        response.set_data(data)
    else:
        data = response.get_data()

    if cacheable and request.method in ('GET', 'HEAD'):
        etag = 'W/"' + hashlib.sha256(data).hexdigest()[:16] + '"'
        response.headers['ETag'] = etag
        inm = request.headers.get('If-None-Match', '')
        if inm and (inm.strip() == '*' or any(tag.strip() == etag for tag in inm.split(','))):
            response.set_data(b'')
            response.status_code = 304
            response.headers.pop('Content-Length', None)
            response.headers.pop('Content-Type', None)
            return response

    _append_vary(response, 'Accept-Encoding')
    if 'Content-Encoding' in response.headers or len(data) < MIN_COMPRESS_BYTES:
        return response

    encoding = choose_encoding(request.headers.get('Accept-Encoding', ''))
    if not encoding:
        return response

    compressed = compress_bytes(data, encoding)
    if len(compressed) >= len(data):
        return response

    response.set_data(compressed)
    response.headers['Content-Encoding'] = encoding
    response.headers['Content-Length'] = str(len(compressed))
    return response
