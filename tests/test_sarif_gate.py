"""Regression shapes from actual GitHub CodeQL main analyses, without source text."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/check_sarif.py'
spec = importlib.util.spec_from_file_location('check_sarif',SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def actual_shape(rule_id='py/insecure-protocol', score='7.5'):
    # Main analysis 1908987756 (Python) and 1908985915 (JavaScript) store rules
    # in tool.extensions[0]; result.rule references both component/rule indices.
    index = 16 if rule_id.startswith('py/') else 36
    rules = [{'id':'unrelated/'+str(number)} for number in range(index)]
    rules.append({'id':rule_id,'properties':{'security-severity':score,'tags':['security','external/cwe/cwe-327']}})
    return {'version':'2.1.0','runs':[{'tool':{'driver':{'name':'CodeQL','semanticVersion':'2.27.2'},
        'extensions':[{'name':'codeql/queries','rules':rules}]},'results':[{
            'ruleId':rule_id,'rule':{'id':rule_id,'toolComponent':{'index':0},'index':index},'level':'warning'}]}]}


class SarifSecurityGateTests(unittest.TestCase):
    def test_real_python_and_javascript_extension_shapes_fail_even_at_warning_level(self):
        for rule_id,score in [('py/insecure-protocol','7.5'),('js/incomplete-multi-character-sanitization','7.8')]:
            with self.subTest(rule=rule_id):
                self.assertEqual(gate.high_findings(actual_shape(rule_id,score)),[rule_id])

    def test_driver_rule_index_and_unique_id_in_extension_both_resolve(self):
        document=actual_shape();run=document['runs'][0]
        run['tool']['driver']['rules']=run['tool'].pop('extensions')[0]['rules']
        run['results'][0]={'ruleIndex':16,'ruleId':'py/insecure-protocol'}
        self.assertEqual(gate.high_findings(document),['py/insecure-protocol'])
        document=actual_shape();document['runs'][0]['results'][0].pop('rule')
        self.assertEqual(gate.high_findings(document),['py/insecure-protocol'])

    def test_component_name_and_guid_reference(self):
        document=actual_shape();run=document['runs'][0]
        extension=run['tool']['extensions'][0];extension['guid']='fixture-guid'
        run['results'][0]['rule']['toolComponent']={'name':extension['name'],'guid':'fixture-guid'}
        self.assertEqual(gate.high_findings(document),['py/insecure-protocol'])

    def test_unresolved_ambiguous_or_conflicting_reference_fails_closed(self):
        for mutation in [
            lambda run:run['results'][0]['rule'].update(toolComponent={'index':8}),
            lambda run:run['results'][0]['rule'].update(index=900),
            lambda run:run['results'][0]['rule'].update(id='wrong'),
            lambda run:run['tool']['extensions'][0]['rules'][-1].update(id='wrong'),
            lambda run:run['results'][0]['rule'].update(toolComponent={'index':0,'name':'wrong'}),
        ]:
            document=actual_shape();mutation(document['runs'][0])
            with self.assertRaises(ValueError):gate.high_findings(document)
        document=actual_shape();run=document['runs'][0];run['results'][0].pop('rule')
        run['tool']['extensions'].append(copy.deepcopy(run['tool']['extensions'][0]))
        with self.assertRaises(ValueError):gate.high_findings(document)

    def test_unknown_nonfinite_and_invalid_security_scores_fail_closed(self):
        for score in [None,'unknown','NaN','Infinity',-1,11,True]:
            with self.subTest(score=score),self.assertRaises(ValueError):
                gate.high_findings(actual_shape(score=score))
        document=actual_shape();props=document['runs'][0]['tool']['extensions'][0]['rules'][-1]['properties']
        props.pop('security-severity');props['tags']=['external/cwe/cwe-327']
        with self.assertRaises(ValueError):gate.high_findings(document)

    def test_clean_nonsecurity_and_medium_findings_pass_but_failed_run_does_not(self):
        self.assertEqual(gate.high_findings(actual_shape(score='6.9')),[])
        document=actual_shape();run=document['runs'][0];run['tool']['extensions'][0]['rules'][-1]['properties']={}
        self.assertEqual(gate.high_findings(document),[])
        run['results']=[];self.assertEqual(gate.high_findings(document),[])
        run['invocations']=[{'executionSuccessful':False}]
        with self.assertRaises(ValueError):gate.high_findings(document)
        with self.assertRaises(ValueError):gate.high_findings({'runs':[]})

    def test_cli_fails_on_actual_shape_and_missing_sarif(self):
        with tempfile.TemporaryDirectory() as temporary:
            result=subprocess.run([sys.executable,str(SCRIPT),temporary],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            path=Path(temporary)/'python.sarif';path.write_text(json.dumps(actual_shape()))
            result=subprocess.run([sys.executable,str(SCRIPT),temporary],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('py/insecure-protocol',result.stderr)
