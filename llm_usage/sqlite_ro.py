"""Read another application's SQLite file without writing next to it."""
import sqlite3
from pathlib import Path


def _closed_wal(path):
    """A WAL-mode database whose -wal file is gone: its application closed it cleanly and
    every committed page is in the main file."""
    try:
        with open(path,'rb') as f:header=f.read(20)
    except OSError:return False
    return len(header)==20 and header[18]==2 and header[19]==2 and not Path(str(path)+'-wal').exists()


def _immutable(path,timeout):
    return sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=timeout)


def open_readonly(path,timeout=5):
    """immutable for a closed WAL database, mode=ro otherwise.

    A mode=ro connection to a closed WAL database has to create -shm (and -wal) before
    its first read. In a writable folder that leaves files beside the other application's
    database; under the collector's read-only home it fails ("attempt to write a readonly
    database"). Without a -wal file every committed page is in the main file, so it is
    read as immutable instead. A writer that reopens the database writes to a new -wal,
    not to the main file; callers that checkpoint on the (db, wal) signature therefore
    read it again on the next pass.

    Any other failure (a database locked by its writer) is raised: reading around a lock
    could return a half-written page."""
    path=Path(path).resolve()
    if _closed_wal(path):return _immutable(path,timeout)
    con=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=timeout)
    try:
        con.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchall()
        return con
    except sqlite3.OperationalError as exc:
        con.close()
        # The -wal file can vanish between the header check and this read.
        if 'readonly' not in str(exc).lower() or Path(str(path)+'-wal').exists():raise
    except BaseException:
        con.close();raise
    return _immutable(path,timeout)
