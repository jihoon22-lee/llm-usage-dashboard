"""Advisor ownership, detail coverage and cache pricing must survive replay."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from llm_usage.claude_usage import components
from llm_usage.cloud import _ingest
from llm_usage.collect import collect_file, model_provider
from llm_usage.insights import token_total
from llm_usage.pricing import estimate
from llm_usage.reconciliation import supplements
from llm_usage.store import Store, identity, stamp

NOW=stamp('2026-10-23T03:00:00Z')
RATES={'claude-parent':dict(input=1,output=2,cache_write=1.25,cache_write_1h=2),
       'claude-advisor':dict(input=2,output=4)}


def message():
    usage=dict(input_tokens=100,output_tokens=40,cache_creation_input_tokens=30,cache_read_input_tokens=0,
               output_tokens_details=dict(thinking_tokens=25),
               cache_creation=dict(ephemeral_5m_input_tokens=10,ephemeral_1h_input_tokens=20),
               iterations=[dict(type='message',input_tokens=100,output_tokens=40),
                           dict(type='advisor_message',model='claude-advisor',input_tokens=300,output_tokens=50)])
    return dict(type='assistant',timestamp='2026-10-23T01:00:00Z',sessionId='detail-session',cwd='/fixture/project',
                message=dict(id='parent',model='claude-parent',content=[dict(type='text',text='NEVER-PERSIST-CONTENT')],usage=usage))


class DetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'usage.db')

    def collect(self,value,name='one.jsonl'):
        path=self.root/name;path.write_text(json.dumps(value)+'\n')
        return collect_file(self.store,path,'claude-code')

    def parent(self):
        with self.store.connect() as c:return dict(c.execute('SELECT * FROM events WHERE parent_id IS NULL').fetchone())

    def test_advisor_added_once_with_its_own_model_without_increasing_api_requests(self):
        value=message();self.assertEqual(self.collect(value),0)
        self.collect(value,'copy.jsonl');self.store=Store(self.root/'usage.db');self.collect(value)
        data=self.store.usage(period='all',now=NOW,pricing=RATES)
        self.assertEqual((token_total(data['totals']),data['totals']['requests'],data['totals']['advisor_calls']),(520,1,1))
        self.assertEqual((data['totals']['output'],data['totals']['reasoning']),(90,25))
        self.assertEqual((data['totals']['cache_creation'],data['totals']['cache_creation_5m'],data['totals']['cache_creation_1h']),(30,10,20))
        self.assertAlmostEqual(data['cost']['period'],(100+80+12.5+40+600+200)/1e6)
        advisor=next(r for r in data['rows'] if r['model']=='claude-advisor')
        self.assertEqual((advisor['requests'],advisor['advisor_calls'],token_total(advisor)),(0,1,350))
        self.assertTrue(self.store.rollup_matches())
        self.assertEqual(data['sessions'][0]['advisor_calls'],1)
        with self.store.connect() as c:self.assertNotIn('NEVER-PERSIST-CONTENT','\n'.join(c.iterdump()))

    def test_cloud_and_local_share_identity_and_model_totals_subtract_advisor(self):
        value=message();self.collect(value)
        item=dict(id='cloud',created_at='2026-10-23T00:00:00Z')
        result=dict(type='result',session_id='detail-session',modelUsage={
            'claude-parent':dict(inputTokens=100,cacheReadInputTokens=0,cacheCreationInputTokens=30,outputTokens=40),
            'claude-advisor':dict(inputTokens=300,cacheReadInputTokens=0,cacheCreationInputTokens=0,outputTokens=50)})
        with self.store.connect() as c:
            _ingest(self.store,c,None,{},item,model_provider,entries=[dict(payload=value),dict(payload=result,created_at='2026-10-23T02:00:00Z')],account='claude-code:test')
            self.assertEqual(supplements(c)[0],[])
            rows=[tuple(r) for r in c.execute('SELECT account,session,requests,advisor_calls FROM events ORDER BY requests DESC')]
        self.assertEqual(rows,[('claude-code:test','detail-session',1,0),('claude-code:test','detail-session',0,1)])
        self.collect(value,'local-again.jsonl')
        self.assertEqual(token_total(self.store.usage(period='all',now=NOW)['totals']),520)

    def test_unknown_detail_and_observed_zero_are_distinct(self):
        value=message();usage=value['message']['usage'];usage.pop('output_tokens_details');usage.pop('cache_creation');usage.pop('iterations')
        self.collect(value)
        row=self.parent();self.assertEqual((row['reasoning_observed'],row['cache_ttl_observed'],row['cache_creation_unknown']),(0,0,30))
        usage['output_tokens_details']={'thinking_tokens':0}
        usage['cache_creation']={'ephemeral_5m_input_tokens':0,'ephemeral_1h_input_tokens':30}
        self.collect(value)
        row=self.parent();self.assertEqual((row['reasoning'],row['reasoning_observed'],row['cache_creation_unknown']),(0,1,0))

    def test_new_larger_snapshot_without_ttl_does_not_reuse_a_stale_split(self):
        value=message();self.collect(value)
        usage=value['message']['usage'];usage['cache_creation_input_tokens']=50;usage.pop('cache_creation')
        self.collect(value)
        row=self.parent();self.assertEqual((row['cache_creation'],row['cache_creation_5m'],row['cache_creation_1h'],row['cache_creation_unknown']),(50,0,0,50))
        self.collect(message(),'older-copy.jsonl')
        self.assertEqual(self.parent()['cache_creation_unknown'],50)

    def test_conflicting_ttl_splits_are_unknown_not_component_maxima(self):
        value=message();self.collect(value)
        value['message']['usage']['cache_creation']={'ephemeral_5m_input_tokens':20,'ephemeral_1h_input_tokens':10}
        self.collect(value)
        row=self.parent();self.assertEqual((row['cache_creation_5m'],row['cache_creation_1h'],row['cache_creation_unknown'],row['cache_ttl_conflict']),(0,0,30,1))
        self.collect(message(),'old-copy.jsonl')
        self.assertEqual(self.parent()['cache_creation_unknown'],30)

    def test_invalid_details_are_reported_and_valid_copy_can_recover(self):
        value=message();value['message']['usage']['cache_creation']['ephemeral_1h_input_tokens']=31
        self.assertEqual(self.collect(value),0)
        self.assertEqual(self.parent()['cache_creation_unknown'],30)
        self.assertEqual(token_total(self.store.usage(period='all',now=NOW)['totals']),520)
        with self.store.connect() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM import_errors WHERE source='claude-details'").fetchone()[0],1)
        self.assertEqual(self.collect(message()),0)
        with self.store.connect() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM import_errors WHERE source='claude-details'").fetchone()[0],0)
        self.assertEqual(token_total(self.store.usage(period='all',now=NOW)['totals']),520)

    def test_old_reasoning_field_and_old_custom_cache_rate_keep_working(self):
        values=components(dict(input_tokens=1,output_tokens=40,reasoning_output_tokens=25,cache_creation_input_tokens=10))
        self.assertEqual(values['reasoning'],25)
        self.assertEqual(values['reasoning_observed'],1)
        self.assertAlmostEqual(estimate(values,dict(input=1,output=2,cache_write=3)),111/1e6)
        from llm_usage.pricing import edited_rates,rate_for
        history=[dict(since='2026-01-01',input=1,output=2,cache_write=3,cache_write_1h=4)]
        changed=edited_rates(history,{},'2026-10-23',remove={'cache_write_1h'})
        self.assertEqual(rate_for('m',{'m':changed},'2026-10-22')['cache_write_1h'],4)
        self.assertNotIn('cache_write_1h',rate_for('m',{'m':changed},'2026-10-23'))

    def test_advisor_only_model_rollup_is_kept_after_update_and_delete(self):
        value=message();self.collect(value)
        value['message']['usage']['iterations'][1]['output_tokens']=60
        self.collect(value)
        self.assertTrue(self.store.rollup_matches())
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT requests,advisor_calls,output FROM usage_hourly WHERE model='claude-advisor'").fetchone()[:],(0,1,60))
            c.execute('DELETE FROM events WHERE parent_id IS NOT NULL')
        self.assertTrue(self.store.rollup_matches())

    def test_zero_executor_with_advisor_is_one_request(self):
        value=message();usage=value['message']['usage']
        for k in ('input_tokens','output_tokens','cache_creation_input_tokens'):usage[k]=0
        usage.pop('output_tokens_details');usage.pop('cache_creation')
        self.collect(value)
        data=self.store.usage(period='all',now=NOW)
        self.assertEqual((token_total(data['totals']),data['totals']['requests'],data['totals']['advisor_calls']),(350,1,1))

    def test_weekly_and_monthly_costs_use_the_same_ttl_and_advisor_parts(self):
        from llm_usage import reports,monthly
        value=message();value['timestamp']='2026-09-15T01:00:00Z';self.collect(value)
        now=stamp('2026-10-01T03:00:00Z')
        usage=self.store.usage(period='all',now=now,pricing=RATES)
        weekly=reports.build(self.store,stamp('2026-09-21T03:00:00Z'),RATES)
        month=monthly.build(self.store,monthly.shift(monthly.month_start(now),-1),now,RATES,{'claude-code':100},{})
        for result in (weekly,month):
            self.assertEqual((result['totals']['tokens'],result['totals']['requests'],result['totals']['advisor_calls']),(520,1,1))
            self.assertAlmostEqual(result['totals']['cost'],usage['cost']['period'])
            self.assertEqual(result['totals']['cache_creation_1h'],20)


if __name__=='__main__':unittest.main()
