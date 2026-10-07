"""Reject high/critical SARIF findings and unresolved security metadata."""
import json
import math
from pathlib import Path
import sys


def _indexed(values, index, description):
    if type(index) is not int or not 0 <= index < len(values):
        raise ValueError('Invalid ' + description + ' index')
    return values[index]


def resolve_rule(run, result):
    """Resolve SARIF 2.1 reportingDescriptorReference, including extension rules."""
    tool = run['tool']
    driver = tool['driver']
    extensions = tool.get('extensions', [])
    components = [driver, *extensions]
    reference = result.get('rule', {})
    identity = reference.get('id', result.get('ruleId'))
    if 'id' in reference and 'ruleId' in result and reference['id'] != result['ruleId']:
        raise ValueError('Conflicting SARIF rule identifiers')
    index = reference.get('index', result.get('ruleIndex'))
    if 'index' in reference and 'ruleIndex' in result and reference['index'] != result['ruleIndex']:
        raise ValueError('Conflicting SARIF rule indices')
    component_ref = reference.get('toolComponent')
    if component_ref is not None:
        if 'index' in component_ref:
            # SARIF toolComponent.index addresses tool.extensions, not the driver.
            component = _indexed(extensions, component_ref['index'], 'tool component')
            if any(component.get(key) != component_ref[key] for key in ('name','guid') if key in component_ref):
                raise ValueError('Conflicting SARIF tool component identity')
            components = [component]
        else:
            keys = [key for key in ('name','guid') if key in component_ref]
            components = [component for component in components
                          if keys and all(component.get(key) == component_ref[key] for key in keys)]
            if len(components) != 1:
                raise ValueError('Unresolved or ambiguous SARIF tool component')
    elif index is not None:
        # Without a component reference, ruleIndex refers to the driver's rules.
        components = [driver]
    candidates = []
    for component in components:
        rules = component.get('rules', [])
        if index is not None:
            rule = _indexed(rules, index, 'rule')
            if identity is not None and rule.get('id') != identity:
                raise ValueError('SARIF rule index does not match its identifier')
            if 'guid' in reference and rule.get('guid') != reference['guid']:
                raise ValueError('SARIF rule index does not match its GUID')
            candidates.append(rule)
        else:
            candidates.extend(rule for rule in rules
                              if (identity is not None or 'guid' in reference)
                              and (identity is None or rule.get('id') == identity)
                              and ('guid' not in reference or rule.get('guid') == reference['guid']))
    if len(candidates) != 1:
        raise ValueError('Unresolved or ambiguous SARIF rule metadata')
    return candidates[0]


def high_findings(document):
    runs = document['runs']
    if not isinstance(runs, list) or not runs:
        raise ValueError('SARIF contains no analysis runs')
    findings = []
    for run in runs:
        for invocation in run.get('invocations', []):
            if invocation.get('executionSuccessful') is False:
                raise ValueError('SARIF analysis did not complete successfully')
        # Validate the tool even for a clean result set.
        if not isinstance(run['tool']['driver'], dict):
            raise ValueError('Missing SARIF tool driver')
        for result in run.get('results', []):
            rule = resolve_rule(run, result)
            properties = rule.get('properties', {})
            tags = properties.get('tags', [])
            security = 'security' in tags or any(tag.startswith('external/cwe/') for tag in tags)
            raw_score = properties.get('security-severity')
            if raw_score is None:
                if security:
                    raise ValueError('Security rule has no security-severity: ' + rule['id'])
                continue
            try:
                score = float(raw_score)
            except (ValueError, TypeError) as error:
                raise ValueError('Invalid security-severity: ' + rule['id']) from error
            if isinstance(raw_score, bool) or not math.isfinite(score) or not 0 <= score <= 10:
                raise ValueError('Invalid security-severity: ' + rule['id'])
            if score >= 7:
                findings.append(rule['id'])
    return findings


def main(folder):
    files = sorted(Path(folder).glob('*.sarif'))
    if not files:
        raise ValueError('CodeQL produced no SARIF results')
    findings = [finding for path in files for finding in high_findings(json.loads(path.read_text()))]
    if findings:
        raise ValueError(f'CodeQL found {len(findings)} high/critical security findings: ' + ', '.join(sorted(set(findings))))
    print('No high/critical CodeQL security findings')


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise SystemExit('CodeQL security gate failed: ' + str(error)) from None
