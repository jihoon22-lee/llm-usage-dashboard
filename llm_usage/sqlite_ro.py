"""Read another application's SQLite file without writing next to it."""
import sqlite3
from pathlib import Path


def open_readonly(path,timeout=5):
    """mode=ro first; immutable only for a closed WAL database.

    The collector runs with a read-only home. When an application closes a WAL
    database it removes the -wal/-shm files, and a mode=ro connection then has to
    create -shm before its first read, which fails there ("attempt to write a
    readonly database"). Without a -wal file every committed page is in the main
    file, so it is read as immutable instead. A writer that reopens the database
    writes to a new -wal, not to the main file; callers that checkpoint on the
    (db, wal) signature therefore read it again on the next pass."""
    path=Path(path).resolve()
    con=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=timeout)
    try:
        con.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchall()
        return con
    except sqlite3.OperationalError:
        con.close()
        if Path(str(path)+'-wal').exists():raise
    except BaseException:
        con.close();raise
    return sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=timeout)
