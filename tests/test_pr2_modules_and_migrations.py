"""Tests for PR 2: backend subpackage layout, PRAGMA user_version migrations, and API type contracts."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from llm_usage import api_types
from llm_usage.db.migrations import SCHEMA_VERSION, current_version, migrate
from llm_usage.providers import antigravity, claude, codex, devin, http, opencode
from llm_usage.store import Store


class MigrationTests(unittest.TestCase):
    def test_fresh_store_sets_schema_user_version(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'usage.db')
            with store.connect() as c:
                self.assertEqual(current_version(c), SCHEMA_VERSION)
                # Running migrate again is a fast no-op.
                self.assertEqual(migrate(c), SCHEMA_VERSION)

    def test_legacy_v0_database_upgrades_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'legacy.db'
            conn = sqlite3.connect(db)
            conn.executescript(
                '''
                CREATE TABLE events (
                  id TEXT PRIMARY KEY, ts REAL NOT NULL, provider TEXT NOT NULL,
                  route TEXT NOT NULL, model TEXT NOT NULL,
                  uncached_input INTEGER NOT NULL, cached_input INTEGER NOT NULL,
                  output INTEGER NOT NULL, cache_creation INTEGER NOT NULL, reasoning INTEGER NOT NULL,
                  session TEXT, project TEXT, agent_kind TEXT);
                INSERT INTO events VALUES ('legacy-1', 1760000000.0, 'Anthropic', 'claude-code', 'claude-sonnet-4-5',
                  100, 200, 50, 40, 10, 's1', 'proj', 'main');
                '''
            )
            conn.commit()
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            conn.close()

            store = Store(db)
            with store.connect() as c:
                self.assertEqual(current_version(c), SCHEMA_VERSION)
                row = c.execute("SELECT * FROM events WHERE id='legacy-1'").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row['output'], 50)
                self.assertEqual(row['cache_creation_unknown'], 40)
                self.assertEqual(row['reasoning_observed'], 1)
                self.assertEqual(row['requests'], 1)


class ModuleSplitTests(unittest.TestCase):
    def test_provider_modules_and_api_types_are_importable(self):
        self.assertTrue(callable(claude.poll_claude))
        self.assertTrue(callable(codex.codex_limits))
        self.assertTrue(callable(antigravity.poll_antigravity_app))
        self.assertTrue(callable(devin.poll_devin))
        self.assertTrue(callable(opencode.go_limits))
        self.assertTrue(callable(http.account_poll))
        for name in (
            'LimitsResponse',
            'OverviewResponse',
            'UsageResponse',
            'ReportsResponse',
            'CollectionStatusResponse',
        ):
            self.assertTrue(hasattr(api_types, name))
