"""Shared validation, query-bound and privacy-safe diagnostic helpers."""

import ipaddress
import logging
import math
from pathlib import Path
import re
import socket
import traceback
from urllib.parse import urlsplit

LOGGER = logging.getLogger('llm_usage')
_PRIVATE_HOST_SUFFIXES = ('.localhost', '.local', '.internal', '.home.arpa')


def is_finite_number(value):
    """Finite int/float, excluding bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_nonnegative_int(value, *, limit=2**63 - 1):
    """Exact int in [0, limit], excluding bool."""
    return type(value) is int and 0 <= value <= limit


def prefix_bounds(prefix):
    """(lo, hi) for an indexed SQLite range scan `col >= lo AND col < hi`."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def is_public_ip(value):
    """True only for globally routable IP addresses (never loopback, private, link-local or CGNAT)."""
    ip = ipaddress.ip_address(value) if isinstance(value, str) else value
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def validate_https_url(
    value, *, reject_private=False, error_message='유효한 HTTPS 주소가 아닙니다.', splitter=urlsplit
):
    """Parse an HTTPS URL without credentials; optionally reject non-public hosts."""
    try:
        if (
            not isinstance(value, str)
            or not value
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
            or '\\' in value
        ):
            raise ValueError
        parts = splitter(value)
        if parts.scheme != 'https' or not parts.netloc or any(ch in parts.netloc for ch in '@%'):
            raise ValueError
        host = parts.hostname
        if not host or parts.netloc.endswith(':') or (parts.port is not None and not 1 <= parts.port <= 65535):
            raise ValueError
        if ':' in host:
            if not re.fullmatch(r'\[[0-9A-Fa-f:.]+\](?::[0-9]+)?', parts.netloc):
                raise ValueError
            ip = ipaddress.IPv6Address(host)
            if reject_private and not is_public_ip(ip):
                raise ValueError
        else:
            if not re.fullmatch(r'[^:\[\]]+(?::[0-9]+)?', parts.netloc):
                raise ValueError
            ascii_host = host.encode('idna').decode('ascii').removesuffix('.').lower()
            if len(ascii_host) > 253 or not all(
                re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in ascii_host.split('.')
            ):
                raise ValueError
            if reject_private:
                try:
                    ip = ipaddress.ip_address(ascii_host)
                except ValueError:
                    if ascii_host == 'localhost' or ascii_host.endswith(_PRIVATE_HOST_SUFFIXES):
                        raise ValueError from None
                else:
                    if not is_public_ip(ip):
                        raise ValueError
        return parts
    except (ValueError, UnicodeError):
        raise ValueError(error_message) from None


def check_public_host(host, port=443):
    """Verify every DNS answer for `host` is a globally routable IP address."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not infos:
            raise ValueError('호스트 주소를 확인할 수 없습니다.') from None
        for info in infos:
            if not is_public_ip(info[4][0]):
                raise ValueError('사설 또는 로컬 네트워크 주소에는 알림을 보낼 수 없습니다.') from None
    else:
        if not is_public_ip(ip):
            raise ValueError('사설 또는 로컬 네트워크 주소에는 알림을 보낼 수 없습니다.')


def log_exception(context, exc, *, logger=LOGGER):
    """Log exception type and source location only — never the message, URLs or secrets."""
    tb = traceback.extract_tb(exc.__traceback__)
    if tb:
        frame = tb[-1]
        logger.warning('%s failed: %s at %s:%d', context, type(exc).__name__, Path(frame.filename).name, frame.lineno)
    else:
        logger.warning('%s failed: %s', context, type(exc).__name__)
