"""Loopback-only OTLP/HTTP JSON receiver; persist allowlisted numeric usage only.

Unmatched request IDs are quarantined as numeric observations, never blindly
added to file-based totals. They are resolved when a transcript supplies an alias.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import math
from pathlib import Path
import re
import time

from .store import identity, TOKENS
from .reconciliation import aliases,link,number

MAX_BODY=2*1024*1024
SAFE=re.compile(r'^[A-Za-z0-9._:/() -]{1,200}$')


def attrs(items):
    out={}
    for item in items or []:
        value=item.get('value') or {}
        for kind in ('stringValue','intValue','doubleValue'):
            if kind in value:out[item.get('key')]=value[kind];break
    return out


def integer(data,*keys):
    for key in keys:
        if key in data:
            v=data[key]
            if isinstance(v,str) and v.isdigit():v=int(v)
            if not number(v):raise ValueError('invalid counter')
            return v
    return None


def resolve(store,c):
    resolved=0
    for row in c.execute('SELECT * FROM telemetry_pending').fetchall():
        found=c.execute('SELECT event_id FROM usage_aliases WHERE route=? AND alias=?',(row['route'],row['alias'])).fetchone()
        if not found:continue
        old=c.execute('SELECT * FROM events WHERE id=?',(found[0],)).fetchone()
        if not old:continue
        store.event(c,old['id'],old['ts'],old['provider'],old['route'],old['model'],json.loads(row['parts']),
                    session=old['session'],project=old['project'],agent_kind=old['agent_kind'])
        link(c,'telemetry:'+str(row['session']),old['id'],'telemetry','final')
        c.execute('DELETE FROM telemetry_pending WHERE id=?',(row['id'],));resolved+=1
    if resolved:
        receipt=store.state(c,'telemetry:received')
        if receipt:
            receipt['pending']=c.execute('SELECT COUNT(*) FROM telemetry_pending').fetchone()[0]
            status='partial' if receipt['pending'] or receipt.get('invalid') else 'ok'
            receipt['status']=status
            store.save_state(c,'telemetry:received',receipt)
            # Resolving a local alias is not a fresh network receipt.
            store.source(c,'telemetry',status,
                         f"숫자 수신 {receipt.get('accepted',0)} · 요청 연결 대기 {receipt['pending']} · 유효하지 않은 항목 {receipt.get('invalid',0)}",
                         receipt['checked'])
    return resolved


def ingest(store,data,now=None):
    now=now or time.time();accepted=ignored=invalid=0
    with store.connect() as c:
        for resource in data.get('resourceLogs') or []:
            common=attrs((resource.get('resource') or {}).get('attributes'))
            for scope in resource.get('scopeLogs') or []:
                for record in scope.get('logRecords') or []:
                    v={**common,**attrs(record.get('attributes'))}
                    name=v.get('event.name') or (record.get('body') or {}).get('stringValue')
                    if name not in ('claude_code.api_request','api_request','codex.sse_event'):
                        ignored+=1;continue
                    route='codex' if name=='codex.sse_event' else 'claude-code'
                    if route=='codex' and v.get('kind')!='response.completed':ignored+=1;continue
                    try:
                        model=v.get('model');rid=v.get('request_id') or v.get('response_id');mid=v.get('message.id')
                        sid=v.get('session.id') or v.get('conversation_id')
                        if not model or not SAFE.fullmatch(model) or not (rid or mid):raise ValueError('identity required')
                        if any(x is not None and (not isinstance(x,str) or not SAFE.fullmatch(x)) for x in (rid,mid,sid)):
                            raise ValueError('invalid identity')
                        ts=int(record['timeUnixNano'])/1e9
                        if not math.isfinite(ts) or not 0<ts<=now+60:raise ValueError('invalid time')
                        inp=integer(v,'input_tokens','input_token_count')
                        out=integer(v,'output_tokens','output_token_count')
                        cache=integer(v,'cache_read_tokens','cached_input_tokens','cached_input_token_count')
                        write=integer(v,'cache_creation_tokens','cache_write_input_tokens')
                        # Missing breakdowns are not fabricated. Codex does not
                        # report cache writes in its standard completed event.
                        if write is None and route=='codex':write=0
                        if any(x is None for x in (inp,out,cache,write)):raise ValueError('incomplete usage')
                        if route=='codex':
                            if cache>inp:raise ValueError('invalid cached input')
                            inp-=cache
                        parts=dict(uncached_input=inp,cached_input=cache,output=out,cache_creation=write)
                        key=identity('telemetry',route,rid or mid)
                        if mid and route=='claude-code':
                            event_id=identity(route,mid)
                            aliases(c,route,event_id,rid,mid,v.get('message.uuid'))
                            # A message id is the same canonical identity used by
                            # the local and cloud parsers, even when telemetry wins the race.
                            store.event(c,event_id,ts,'Anthropic',route,model,parts,session=sid)
                            link(c,'claude:'+str(sid),event_id,'telemetry','final')
                        else:
                            c.execute('INSERT OR REPLACE INTO telemetry_pending VALUES (?,?,?,?,?,?,?)',
                                      (key,route,ts,sid,model,identity(rid or mid),json.dumps(parts)))
                        accepted+=1
                    except (ValueError,TypeError,KeyError,OverflowError):invalid+=1
        resolved=resolve(store,c)
        pending=c.execute('SELECT COUNT(*) FROM telemetry_pending').fetchone()[0]
        store.source(c,'telemetry','partial' if invalid or pending else 'ok' if accepted or resolved else 'unavailable',
                     f'숫자 수신 {accepted} · 요청 연결 대기 {pending} · 유효하지 않은 항목 {invalid}',now)
        if accepted or resolved:store.save_state(c,'telemetry:received',dict(checked=now,accepted=accepted,pending=pending,invalid=invalid))
    return dict(accepted=accepted,ignored=ignored,invalid=invalid,resolved=resolved,pending=pending)


def handler(store,token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            if self.path!='/v1/logs':self.send_error(404);return
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):self.send_error(401);return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':self.send_error(415);return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=MAX_BODY:self.send_error(413);return
                self.connection.settimeout(10)
                data=json.loads(self.rfile.read(n))
                if not isinstance(data,dict):raise ValueError('object required')
                outcome=ingest(store,data)
                response={'partialSuccess':{'rejectedLogRecords':str(outcome['invalid']),
                                           'errorMessage':'Invalid usage records'} } if outcome['invalid'] else {}
            except (ValueError,TypeError,AttributeError,OSError):self.send_error(400);return
            body=json.dumps(response).encode();self.send_response(200)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
    return Handler


def serve(store,port,token_file):
    path=Path(token_file)
    if path.stat().st_mode & 0o077:raise ValueError('telemetry token file must be private (0600)')
    token=path.read_text().strip()
    if len(token)<32 or not token.isascii():raise ValueError('telemetry token must have at least 32 ASCII characters')
    with ThreadingHTTPServer(('127.0.0.1',port),handler(store,token)) as server:server.serve_forever()
