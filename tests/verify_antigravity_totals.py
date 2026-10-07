"""Independent, read-only comparison of step-level protobuf numbers to events.

No collector/parser functions are called. Source field numbers were checked
against the installed CLI descriptors. Prompts and headers never leave memory.
Run after collection while sources are stable; a newly arriving request will be
reported as missing rather than silently considered verified.
"""
if __name__ == '__main__':
    import argparse
    from pathlib import Path
    from llm_usage.config import settings
    from llm_usage.verify import _antigravity
    parser = argparse.ArgumentParser(); parser.add_argument('--database', type=Path); args = parser.parse_args()
    config = settings(); result = _antigravity(Path(args.database or config['database']), config); print(result)
    raise SystemExit(any(result[k] for k in ('source_errors', 'source_conflicts', 'output_split_errors',
                                             'missing_events', 'mismatched_events', 'alias_errors')))
