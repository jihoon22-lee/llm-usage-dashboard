"""Request-bound account proof and telemetry copies must not change canonical usage."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from llm_usage.claude_accounts import account_key,UNKNOWN,route_for
from llm_usage.claude_attribution import account_for,resolve as attribute
from llm_usage.collect import collect_file,parse_line
from llm_usage.codex_usage import bind_account
from llm_usage.resources import key,observe_account
from llm_usage.store import Store,identity,stamp
from llm_usage.telemetry import ingest,resolve
from test_native_usage import meta,model,response,point,usage
from test_usage_details import message

NOW=stamp('2026-10-23T03:00:00Z')


def otlp(**overrides):
    values={'event.name':'claude_code.api_request','request_id':'request','session.id':'detail-session',
            'model':'claude-parent','input_tokens':100,'output_tokens':40,'cache_read_tokens':0,'cache_creation_tokens':30,
            'user.account_uuid':'TELEMETRY-USER-ONLY','organization.id':'TELEMETRY-ORG-ONLY','user.email':'NEVER-PERSIST-EMAIL',
            'prompt':'NEVER-PERSIST-PROMPT',**overrides}
    attrs=[dict(key=k,value={'intValue':str(v)} if type(v) is int else {'stringValue':v}) for k,v in values.items() if v is not None]
    record=dict(timeUnixNano=str(int((NOW-10)*1e9)),attributes=attrs)
    return dict(resourceLogs=[dict(scopeLogs=[dict(logRecords=[record])])])


class TelemetryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db')
        self.primary=account_key(('PRIMARY-USER','PRIMARY-ORG'))
        self.owner=account_key(('TELEMETRY-USER-ONLY','TELEMETRY-ORG-ONLY'))
        with self.store.connect() as c:self.store.save_state(c,'claude_primary_account',self.primary)

    def test_sdk_identity_resolves_parent_and_advisor_even_when_telemetry_arrives_first(self):
        self.assertEqual(ingest(self.store,otlp(),NOW)['pending'],1)
        value=message();value['requestId']='request'
        with self.store.connect() as c:
            parse_line(self.store,c,'claude-code',value,{})
            c.execute('UPDATE events SET account=?',(UNKNOWN,))
            self.assertEqual(resolve(self.store,c),1)
        with patch.object(Path,'home',return_value=self.root):
            attribute(self.store,dict(sources=[],claude_auth=str(self.root/'missing/.credentials.json'),claude_discover_homes=False))
        with self.store.connect() as c:
            self.assertEqual({r[0] for r in c.execute('SELECT account FROM events')},{route_for(self.owner,self.primary)})
            self.assertEqual(c.execute('SELECT SUM(requests),SUM(advisor_calls) FROM events').fetchone()[:],(1,1))
            dump='\n'.join(c.iterdump())
            for private in ('TELEMETRY-USER-ONLY','TELEMETRY-ORG-ONLY','NEVER-PERSIST-EMAIL','NEVER-PERSIST-PROMPT'):
                self.assertNotIn(private,dump)

    def test_conflicting_request_identity_does_not_change_usage_or_add_aliases(self):
        first=otlp(**{'message.id':'m'});self.assertEqual(ingest(self.store,first,NOW)['accepted'],1)
        second=otlp(**{'message.id':'m','request_id':'bad-request','user.account_uuid':'OTHER-USER','output_tokens':400})
        self.assertEqual(ingest(self.store,second,NOW)['invalid'],1)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT output,account FROM events').fetchone()[:],(40,route_for(self.owner,self.primary)))
            self.assertIsNone(c.execute('SELECT 1 FROM usage_aliases WHERE alias=?',(identity('bad-request'),)).fetchone())

    def test_anonymous_ids_do_not_become_a_subscription_account(self):
        data=otlp(**{'message.id':'m','user.account_uuid':None,'organization.id':None,'user.id':'random-installation'})
        self.assertEqual(ingest(self.store,data,NOW)['accepted'],1)
        with self.store.connect() as c:
            self.assertIsNone(c.execute('SELECT account FROM events').fetchone()[0])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM claude_evidence').fetchone()[0],0)

    def test_simultaneous_account_evidence_is_not_arbitrarily_ordered(self):
        self.assertEqual(account_for(10,[(10,'claude-code:a'),(10,'claude-code:b')],None,'claude-code'),UNKNOWN)
        self.assertEqual(account_for(20,[(10,'claude-code:a'),(10,'claude-code:b'),(15,'claude-code:a')],None,'claude-code'),'claude-code:a')

    def test_native_codex_copy_counts_cache_writes_once_and_holds_conflicting_telemetry(self):
        rows=[meta(),model(),response('request',usage(100,20,10,5)),point(usage(100,20,10,5))]
        path=self.root/'native.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows));collect_file(self.store,path,'codex')
        good=otlp(**{'event.name':'codex.sse_event','kind':'response.completed','input_tokens':100,'output_tokens':20,
                     'cache_read_tokens':10,'cache_creation_tokens':5})
        self.assertEqual(ingest(self.store,good,NOW)['resolved'],1)
        bad=otlp(**{'event.name':'codex.sse_event','kind':'response.completed','input_tokens':101,'output_tokens':20,
                    'cache_read_tokens':10,'cache_creation_tokens':5})
        result=ingest(self.store,bad,NOW);self.assertEqual((result['pending'],result['conflicts']),(1,1))
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT uncached_input,cached_input,cache_creation,output FROM events').fetchone()[:],(85,10,5,20))
        result=ingest(self.store,good,NOW);self.assertEqual((result['resolved'],result['conflicts']),(1,0))

    def test_proven_other_codex_account_does_not_subtract_from_current_account_total(self):
        with self.store.connect() as c:
            owner=observe_account(c,'codex',('creator-a',None),NOW)
            bind_account(self.store,c,('creator-a',None),owner['account_key'],NOW)
            bind_account(self.store,c,('creator-b',None),key('codex',('creator-b',None)),NOW)
            for name,amount in [('creator-a',120),('creator-b',240)]:
                self.store.event(c,name,NOW-30,'OpenAI','codex','gpt-test',dict(output=amount),account='codex-account:'+identity(name))
            self.store.account_tokens(c,'codex',[dict(startDate='2026-10-23',tokens=150)],NOW)
            gap=self.store.account_gap(c,'codex',NOW)
            self.assertEqual((gap['days'][0]['local'],gap['total_gap']),(120,30))
        data=self.store.usage(period='today',now=NOW)
        self.assertEqual(data['reconciliation']['residual_tokens'],30)
        # A creator shared across workspace identities is insufficient proof.
        with self.store.connect() as c:
            bind_account(self.store,c,('creator-b','another-workspace'),key('codex',('creator-b','another-workspace')),NOW)
            self.assertEqual(self.store.account_gap(c,'codex',NOW)['days'][0]['local'],360)


if __name__=='__main__':unittest.main()
