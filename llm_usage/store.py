"""Only numeric usage, opaque identities and collection metadata enter SQLite."""

from collections import OrderedDict
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
import time  # noqa: F401 - tests patch llm_usage.store.time.time

from .db.events import EventsMixin
from .db.limits_query import LimitsMixin
from .db.migrations import SCHEMA_VERSION, current_version, migrate
from .db.schema import (
    BUCKET_SQL,
    DETAILS,
    EFFORT_LEVELS,
    GENERIC_FOLDERS,
    HISTORY_FIELDS,
    KST,
    METRICS,
    NAMED_EFFORT_ROUTES,
    ONE_TIME_SOURCES,
    PROJECTION_FROM_DAY,
    QUOTA_PARENTS,
    SERIES_KEYS,
    STATUS_LINE_SOURCES,
    SUPPORTED_ROUTES,
    TOKENS,
    USER_ENDED_DETAIL,
    calendar_buckets,
    clean_label,
    identity,
    model_effort,
    project_label,
    repository_root,
    route_events,
    stamp,
)
from .db.usage_query import UsageMixin

__all__ = [
    'BUCKET_SQL',
    'DETAILS',
    'EFFORT_LEVELS',
    'GENERIC_FOLDERS',
    'HISTORY_FIELDS',
    'KST',
    'METRICS',
    'NAMED_EFFORT_ROUTES',
    'ONE_TIME_SOURCES',
    'PROJECTION_FROM_DAY',
    'QUOTA_PARENTS',
    'SCHEMA_VERSION',
    'SERIES_KEYS',
    'STATUS_LINE_SOURCES',
    'SUPPORTED_ROUTES',
    'Store',
    'TOKENS',
    'USER_ENDED_DETAIL',
    'calendar_buckets',
    'clean_label',
    'current_version',
    'identity',
    'migrate',
    'model_effort',
    'project_label',
    'repository_root',
    'route_events',
    'stamp',
]


class Store(EventsMixin, LimitsMixin, UsageMixin):
    # unused_*: the pre-reset notice (off unless its notification is turned on) for a window
    # longer than a day with at least unused_percent left within unused_hours of its reset.
    DEFAULT_THRESHOLDS = {
        'stale_seconds': 600,
        'retention_days': 30,
        'low_percent': 15,
        'quota_hide_days': 7,
        'unused_percent': 50,
        'unused_hours': 48,
    }

    def __init__(self, path, thresholds=None, rollup=True):
        self.path = str(path)
        # Whole-hour aggregations read usage_hourly; False reads events (equivalence checks).
        self.table = 'usage_hourly' if rollup else 'events'
        self.rcount = 'COALESCE(SUM(requests),0) AS requests'
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._memo = {}
        self._usage_cache = OrderedDict()
        # gunicorn serves requests on several threads that share this Store.
        self._usage_lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            migrate(c)
        self._ensure_rollup()
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA synchronous=NORMAL')
        # REPLACE deletes the conflicting events row; only with recursive triggers does
        # that deletion run the rollup's delete trigger (otherwise it would count twice).
        c.execute('PRAGMA recursive_triggers=ON')
        try:
            with c:
                yield c
        finally:
            c.close()
