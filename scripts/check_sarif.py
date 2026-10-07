"""Fail the security gate for high/critical findings, retaining CodeQL UI details."""
import json
from pathlib import Path
import sys

files = list(Path(sys.argv[1]).glob('*.sarif'))
if not files:
    raise SystemExit('CodeQL produced no SARIF results')
count = 0
for path in files:
    for run in json.loads(path.read_text())['runs']:
        rules = {rule['id']:rule for rule in run['tool']['driver']['rules']}
        for result in run.get('results', []):
            rule = rules.get(result['ruleId'], {})
            score = float(rule.get('properties', {}).get('security-severity', 0))
            if score >= 7:
                count += 1
if count:
    raise SystemExit(f'CodeQL found {count} high/critical security findings; see code scanning')
print('No high/critical CodeQL security findings')
