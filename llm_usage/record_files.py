"""Streaming readers shared by collection and original-record verification."""
import sys
from pathlib import Path

if sys.version_info >= (3,14):
    from compression import zstd
else:
    from backports import zstd

RecordReadError=zstd.ZstdError


def compressed(path):
    return str(path).endswith('.jsonl.zst')


def open_record(path):
    return zstd.open(path,'rb') if compressed(path) else Path(path).open('rb')


def record_files(root):
    return sorted(p for p in Path(root).rglob('*') if p.is_file() and
                  (p.name.endswith('.jsonl') or compressed(p)))


def unsupported_files(root):
    return sum(1 for p in Path(root).rglob('*') if p.is_file() and
               any(p.name.endswith('.jsonl'+suffix) for suffix in ('.gz','.xz','.bz2','.lz4')))
