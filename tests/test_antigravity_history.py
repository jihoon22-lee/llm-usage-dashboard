from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from llm_usage.antigravity import collect_database,collect_databases,message,InvalidProtobuf
from llm_usage.collect import model_provider
from llm_usage.store import Store,stamp


def varint(value):
    out=bytearray()
    while value>127:out.append((value&127)|128);value>>=7
    out.append(value);return bytes(out)


def uint(field,value):return varint(field<<3)+varint(value)


def blob(field,value):
    if isinstance(value,str):value=value.encode()
    return varint((field<<3)|2)+varint(len(value))+value


def ts(seconds):return uint(1,int(seconds))+uint(2,round((seconds-int(seconds))*10**9))


def usage(request='response-1',input=10,cached=90,output=20,creation=7,thinking=5,model=1318,api=24,message_id=None):
    return (uint(1,model)+uint(2,input)+uint(3,output)+uint(4,creation)+uint(5,cached)+uint(6,api)+
            uint(9,thinking)+uint(10,output-thinking)+(blob(11,request) if request else b'')+
            (blob(7,message_id) if message_id else b'')+blob(8,blob(1,'secret-header')+blob(2,'PRIVATE KEY')))


def metadata(value,when,gen=0,complete=True):
    return ((blob(1,ts(when)) if when is not None else b'')+blob(9,value)+
            blob(20,blob(1,'original-trajectory')+uint(3,gen))+
            (blob(8,ts(when+1)) if complete and when else b'')+
            blob(4,'PRIVATE TOOL ARGUMENTS'))


def generator(value,name='gemini-test',model=1318):
    return blob(1,uint(3,model)+blob(4,value)+blob(19,name)+blob(1,'PRIVATE SYSTEM PROMPT')+blob(2,'PRIVATE CONVERSATION'))


class AntigravityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/'conversation.db';self.store=Store(self.root/'usage.db')
        self.when=stamp('2026-09-08T23:59:00+09:00')
        with closing(sqlite3.connect(self.db)) as c,c:
            c.executescript('CREATE TABLE steps (idx INTEGER PRIMARY KEY,metadata BLOB); CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY,data BLOB);')

    def write(self,steps,gens=()):
        with closing(sqlite3.connect(self.db)) as c,c:
            c.executemany('INSERT OR REPLACE INTO steps VALUES (?,?)',steps)
            c.executemany('INSERT OR REPLACE INTO gen_metadata VALUES (?,?)',gens)

    def collect(self,path=None):return collect_database(self.store,path or self.db,model_provider)

    def events(self):
        with self.store.connect() as c:return [dict(r) for r in c.execute("SELECT * FROM events WHERE route='antigravity' ORDER BY ts")]

    def test_requests_belong_to_their_conversation_as_a_hashed_session(self):
        self.write([(0,metadata(usage(),self.when)),(1,metadata(usage('response-2'),self.when+60))])
        self.collect()
        sessions={e['session'] for e in self.events()}
        self.assertEqual(len(sessions),1);session=sessions.pop()
        self.assertTrue(session.startswith('agy-'));self.assertNotIn('conversation',session)
        detail=self.store.session_detail(session)
        self.assertEqual(detail['requests'],2);self.assertIsNone(detail['project'])
        self.assertEqual(self.store.usage(period='all',sections='insights')['sessionless_requests'],0)

    def test_request_steps_not_aggregated_generator_cache_and_thinking(self):
        first=usage();second=usage('response-2',input=30,cached=200,output=40,creation=0,thinking=8)
        aggregate=usage(input=40,cached=290,output=60,creation=7,thinking=13)
        self.write([(0,metadata(first,self.when)),(1,metadata(second,self.when+120))],[(0,generator(aggregate))])
        self.collect();data=self.store.usage(period='all',group='provider')
        self.assertEqual(data['totals'],dict(uncached_input=40,cached_input=290,output=60,cache_creation=7,reasoning=13,requests=2))
        self.assertEqual(data['labels'],['Google']);self.assertEqual(len(self.events()),2)
        today=self.store.usage(period='custom',start='2026-09-09',end='2026-09-09')
        self.assertEqual(today['totals']['output'],40)
        self.assertEqual(data['insights']['cache']['rate'],290/337*100)
        for grain in ('day','week','month'):
            d=self.store.usage(period='all',granularity=grain)
            self.assertEqual(sum(sum(v['output'] for v in point['values'].values()) for point in d['series']),60)
        with self.store.connect() as c:
            stored=' '.join(str(tuple(r)) for r in c.execute('SELECT * FROM antigravity_requests'))
            self.assertNotIn('PRIVATE',stored);self.assertNotIn('response-1',stored)
            self.assertNotIn('original-trajectory',stored)

    def test_streaming_id_upgrade_copies_and_parent_replay(self):
        incomplete=usage(None,output=4,thinking=1,message_id='message-1')
        self.write([(0,metadata(incomplete,self.when,complete=False))],[(0,generator(incomplete))]);self.collect()
        copy=self.root/'windows-copy.db';shutil.copyfile(self.db,copy)
        finished=usage(output=20,thinking=5,message_id='message-1')
        self.write([(0,metadata(finished,self.when))]);self.collect();self.collect(copy)
        # A child/copy can retain only the provider response ID.
        self.write([(9,metadata(usage(),self.when))]);self.collect()
        self.assertEqual(len(self.events()),1);self.assertEqual(self.events()[0]['output'],20)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM antigravity_requests').fetchone()[0],1)
            self.assertGreaterEqual(c.execute('SELECT COUNT(*) FROM antigravity_request_aliases').fetchone()[0],2)

    def test_distinct_requests_counter_decrease_and_model_split(self):
        first=usage();second=usage('response-2',input=2,cached=0,output=3,creation=0,thinking=1,model=999,api=26)
        self.write([(0,metadata(first,self.when)),(1,metadata(second,self.when+1,gen=1))],
                   [(0,generator(first)),(1,generator(second,'claude-test',model=999))]);self.collect()
        self.assertEqual({r['provider']:r['output'] for r in self.events()},{'Google':20,'Anthropic':3})
        self.assertEqual({r['model'] for r in self.events()},{'gemini-test','claude-test'})

    def test_step_only_unknown_model_kept_and_later_named(self):
        self.write([(0,metadata(usage(model=1050),self.when))]);self.collect()
        self.assertEqual(self.events()[0]['provider'],'Google');self.assertEqual(self.events()[0]['model'],'unknown')
        self.write([],[(0,generator(usage(model=1050),'gemini-later',model=1050))]);self.collect()
        self.assertEqual(len(self.events()),1);self.assertEqual(self.events()[0]['model'],'gemini-later')

    def test_missing_identity_timestamp_and_malformed_row_repaired(self):
        self.write([(0,metadata(usage(None),self.when)),(1,metadata(usage('two'),None)),(2,b'\x4a\xff'),
                    (3,metadata(usage('good'),self.when))])
        result=self.collect();self.assertEqual(result['errors'],3);self.assertEqual(len(self.events()),1)
        self.write([(0,metadata(usage('one'),self.when)),(1,metadata(usage('two'),self.when)),
                    (2,metadata(usage('three'),self.when))]);self.collect()
        self.assertEqual(len(self.events()),4)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM import_errors').fetchone()[0],0)

    def test_wal_change_detected_and_source_never_modified(self):
        with closing(sqlite3.connect(self.db)) as source:
            source.execute('PRAGMA journal_mode=WAL');source.commit()
            source.execute('INSERT INTO steps VALUES (?,?)',(0,metadata(usage(),self.when)));source.commit()
            self.collect();before=list(source.execute('SELECT * FROM steps'))
            source.execute('INSERT INTO steps VALUES (?,?)',(1,metadata(usage('second'),self.when+1)));source.commit()
            self.collect()
            self.assertEqual(source.execute('SELECT * FROM steps WHERE idx=0').fetchall(),before)
            self.assertEqual(len(self.events()),2)

    def test_contradictory_snapshots_never_combine_components(self):
        self.write([(0,metadata(usage(input=100,cached=0,output=20,thinking=5),self.when))]);self.collect()
        self.write([(0,metadata(usage(input=50,cached=100,output=30,thinking=5),self.when))]);self.collect()
        row=self.events()[0]
        self.assertEqual((row['uncached_input'],row['cached_input'],row['output']),(50,100,30))
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT conflict FROM antigravity_requests').fetchone()[0],1)

    def test_summary_and_failure_preserve_data(self):
        self.write([(0,metadata(usage(),self.when))],[(0,generator(usage()))])
        settings={'sources':[{'kind':'antigravity','name':'fixture','path':str(self.db)}]}
        collect_databases(self.store,settings,model_provider)
        data=self.store.usage(period='all')
        self.assertEqual(data['unavailable_routes'],[])
        self.assertEqual(next(s for s in data['sources'] if s['name']=='antigravity-records')['status'],'ok')
        corrupt=self.root/'corrupt.db';corrupt.write_bytes(b'not a database')
        settings['sources'][0]['path']=str(corrupt)
        collect_databases(self.store,settings,model_provider)
        self.assertEqual(len(self.events()),1)
        with self.store.connect() as c:self.assertEqual(c.execute("SELECT status FROM sources WHERE name='antigravity-records'").fetchone()[0],'error')

    def test_pending_identity_does_not_invent_zero_usage(self):
        value=uint(1,1318)+uint(6,24)+blob(11,'pending')
        self.write([(0,metadata(value,self.when,complete=False))])
        self.assertEqual(self.collect()['errors'],1);self.assertEqual(self.events(),[])
        self.write([(0,metadata(usage('pending'),self.when))])
        self.assertEqual(self.collect()['errors'],0);self.assertEqual(self.events()[0]['output'],20)

    def test_complete_unknown_model_keeps_consumption_and_reports_name_gap(self):
        self.write([(0,metadata(usage(model=1050),self.when))])
        settings={'sources':[{'kind':'antigravity','name':'fixture','path':str(self.db)}]}
        collect_databases(self.store,settings,model_provider)
        with self.store.connect() as c:
            row=c.execute("SELECT status,detail FROM sources WHERE name='antigravity-records'").fetchone()
            self.assertEqual(row['status'],'ok');self.assertIn('모델 미확인 1개',row['detail'])
        self.assertEqual(self.events()[0]['provider'],'Google')

    def test_wire_limits_and_output_split_reject_invalid_data(self):
        for raw in (b'\x00',b'\x08'+b'\xff'*10,b'\x0a\x05xx',blob(1,'wrong wire')):
            with self.assertRaises(InvalidProtobuf):message(raw,{1:0})
        bad=usage()+uint(10,999)
        self.write([(0,metadata(bad,self.when))]);self.assertEqual(self.collect()['errors'],1)
        self.assertEqual(self.events(),[])


class AntigravityDesktopRootsTests(unittest.TestCase):
    """CLI and desktop-app DBs on WSL and Windows homes, including copied requests."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name);self.when=stamp('2026-10-03T15:40:00+09:00')
        self.wsl=self.base/'wsl';self.win=self.base/'win'
        self.store=Store(self.base/'usage.db')

    def settings(self,homes=None):
        return {'sources':[],'homes':[str(h) for h in (homes or (self.wsl,self.win))]}

    def db(self,home,app,name,steps,gens=()):
        folder=home/('.gemini/antigravity/conversations' if app else '.gemini/antigravity-cli/conversations')
        folder.mkdir(parents=True,exist_ok=True);path=folder/(name+'.db')
        with closing(sqlite3.connect(path)) as c,c:
            c.executescript('CREATE TABLE IF NOT EXISTS steps (idx INTEGER PRIMARY KEY,metadata BLOB);'
                            'CREATE TABLE IF NOT EXISTS gen_metadata (idx INTEGER PRIMARY KEY,data BLOB);')
            c.executemany('INSERT OR REPLACE INTO steps VALUES (?,?)',steps)
            c.executemany('INSERT OR REPLACE INTO gen_metadata VALUES (?,?)',gens)
        return path

    def collect(self,homes=None):collect_databases(self.store,self.settings(homes),model_provider)

    def totals(self):
        with self.store.connect() as c:
            requests=c.execute('SELECT COUNT(*) FROM antigravity_requests').fetchone()[0]
            events=[dict(r) for r in c.execute("SELECT * FROM events WHERE route='antigravity' ORDER BY ts,id")]
        return requests,events

    def source(self,name):
        with self.store.connect() as c:
            row=c.execute('SELECT status,detail FROM sources WHERE name=?',(name,)).fetchone()
        return row and dict(row)

    def test_roots_cover_cli_and_app_per_home_with_distinct_names(self):
        from llm_usage.antigravity import roots
        names=[(n,str(p.relative_to(self.base)),o) for n,p,o in roots(self.settings())]
        self.assertEqual(names,[('WSL Antigravity DB','wsl/.gemini/antigravity-cli/conversations',False),
                                ('WSL Antigravity 앱 DB','wsl/.gemini/antigravity/conversations',True),
                                ('WSL Antigravity DB 2','win/.gemini/antigravity-cli/conversations',False),
                                ('WSL Antigravity 앱 DB 2','win/.gemini/antigravity/conversations',True)])

    def test_app_db_is_collected_and_missing_app_dir_does_not_degrade_summary(self):
        self.db(self.wsl,False,'cli',[(0,metadata(usage('cli-1'),self.when))])
        self.db(self.win,False,'cli',[(0,metadata(usage('cli-2'),self.when))])
        self.collect()
        self.assertEqual(self.source('antigravity-records')['status'],'ok')
        self.assertIsNone(self.source('WSL Antigravity 앱 DB'))
        self.db(self.wsl,True,'app',[(0,metadata(usage('app-1',model=999,api=26),self.when))],
                [(0,generator(usage('app-1',model=999,api=26),'claude-opus-test',model=999))])
        self.collect();requests,events=self.totals()
        self.assertEqual(requests,3);self.assertEqual(self.source('WSL Antigravity 앱 DB')['status'],'ok')
        self.assertEqual(sorted(e['model'] for e in events),['claude-opus-test','unknown','unknown'])
        app=[e for e in events if e['model']=='claude-opus-test'];self.assertEqual(len(app),1)
        self.assertEqual(app[0]['provider'],'Anthropic')
        self.assertEqual(self.source('antigravity-records')['status'],'ok')

    def test_identical_copy_in_wsl_and_windows_app_counts_once(self):
        steps=[(0,metadata(usage('r1'),self.when)),(1,metadata(usage('r2',input=3,output=9,thinking=2),self.when+60))]
        self.db(self.wsl,True,'conv',steps);self.db(self.win,True,'conv',steps)
        self.collect();requests,events=self.totals()
        self.assertEqual(requests,2);self.assertEqual(len(events),2)
        self.assertEqual(self.store.usage(period='all')['totals']['output'],29)

    def test_same_request_in_cli_and_app_db_counts_once(self):
        self.db(self.wsl,False,'cli',[(0,metadata(usage('shared'),self.when))])
        self.db(self.wsl,True,'app',[(4,metadata(usage('shared'),self.when))])
        self.collect();requests,events=self.totals()
        self.assertEqual((requests,len(events),events[0]['output']),(1,1,20))

    def test_streaming_partial_in_wsl_and_completed_copy_in_windows(self):
        self.db(self.wsl,True,'conv',[(0,metadata(usage('stream',output=4,thinking=1),self.when,complete=False))])
        self.db(self.win,True,'conv',[(0,metadata(usage('stream',output=20,thinking=5),self.when))])
        for homes in ((self.wsl,self.win),(self.win,self.wsl)):
            self.collect(homes);requests,events=self.totals()
            self.assertEqual((requests,len(events),events[0]['output']),(1,1,20))
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT conflict FROM antigravity_requests').fetchone()[0],0)

    def test_alias_expansion_across_homes_merges_transitively(self):
        # message-only -> message+response -> response-only must become one request.
        self.db(self.wsl,True,'a',[(0,metadata(usage(None,message_id='m-1'),self.when))])
        self.db(self.win,False,'c',[(0,metadata(usage('resp-1'),self.when))])
        self.collect();self.assertEqual(self.totals()[0],2)
        self.db(self.win,True,'b',[(0,metadata(usage('resp-1',message_id='m-1'),self.when))])
        self.collect();requests,events=self.totals()
        self.assertEqual((requests,len(events)),(1,1))
        self.assertEqual(self.store.usage(period='all')['totals']['output'],20)

    def test_contradictory_copies_are_not_summed_and_flag_conflict(self):
        self.db(self.wsl,True,'conv',[(0,metadata(usage('x',input=100,cached=0,output=20,thinking=5),self.when))])
        self.db(self.win,True,'conv',[(0,metadata(usage('x',input=50,cached=100,output=30,thinking=5),self.when))])
        self.collect();requests,events=self.totals()
        self.assertEqual((requests,len(events)),(1,1))
        # One real snapshot (the larger completed output) is kept; components are never mixed.
        self.assertEqual((events[0]['uncached_input'],events[0]['cached_input'],events[0]['output']),(50,100,30))
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT conflict FROM antigravity_requests').fetchone()[0],1)
        self.assertEqual(self.source('antigravity-records')['status'],'partial')

    def test_recollect_reorder_and_removed_copy_are_idempotent(self):
        steps=[(i,metadata(usage(f'req-{i}',input=i+1,output=10+i,thinking=1),self.when+i)) for i in range(5)]
        self.db(self.wsl,True,'conv',steps);copy=self.db(self.win,True,'conv',steps)
        self.db(self.win,False,'cli',steps[:2])
        self.collect();first=self.totals()
        self.collect();self.collect((self.win,self.wsl))
        self.assertEqual(self.totals(),first);self.assertEqual(first[0],5)
        copy.unlink();self.collect()
        self.assertEqual(self.totals(),first)
        self.assertEqual(self.store.usage(period='all')['totals']['output'],sum(10+i for i in range(5)))
