"""Read-only reconciliation against original records and cumulative observations.

Complete, reset and partial-baseline sessions are verified separately. Conflicting
counters at one timestamp lack a unique chronological order and are reported
separately; fixture tests exercise the collector's conservative tie handling.
"""
import argparse
from pathlib import Path

from llm_usage.config import settings
from llm_usage.verify import failed, partial, reconcile

parser = argparse.ArgumentParser()
parser.add_argument('--database', type=Path)
args = parser.parse_args()
cfg = settings()
result = reconcile(args.database or Path(cfg['database']), cfg)
for failure in result['codex'].pop('mismatch_samples', []):
    print(failure)
for section, counts in result.items():
    print({section: counts})
print({'verification':'failed' if failed(result) else 'partial' if partial(result) else 'matched'})
raise SystemExit(failed(result))
