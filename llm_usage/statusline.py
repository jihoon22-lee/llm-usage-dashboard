"""Standalone Windows/WSL wrapper. Only allowlisted quota/token numbers and opaque identities leave stdin.

The original command receives exactly the original input; its output and exit
status are preserved. This file intentionally depends only on Python stdlib.
"""
from contextlib import closing
import argparse
import hashlib
import sqlite3
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def sanitize(route,data):
    key='rate_limits' if route=='claude-code' else 'quota'
    fields=('used_percentage','resets_at') if route=='claude-code' else ('remaining_fraction','reset_time','reset_in_seconds')
    result={}
    for bucket,row in (data.get(key) or {}).items():
        if not re.fullmatch(r'[a-zA-Z0-9_.:/ -]{1,100}',bucket) or not isinstance(row,dict):continue
        safe={}
        for field in fields:
            value=row.get(field)
            if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):safe[field]=value
            elif field=='reset_time' and isinstance(value,str) and re.fullmatch(r'[0-9TZ:+. -]{10,40}',value):safe[field]=value
        if safe:result[bucket]=safe
    return {key:result}


TOKEN_FIELDS=('total_input_tokens','total_output_tokens','context_window_size')
CURRENT_FIELDS=('input_tokens','output_tokens','cache_creation_input_tokens','cache_read_input_tokens')


def token_fields(value,fields):
    if not isinstance(value,dict):return {}
    return {key:value[key] for key in fields if key in value and
            (value[key] is None or (type(value[key]) is int and 0<=value[key]<=2**63-1))}


def field_state(data,key):
    return 'missing' if key not in data else 'null' if data[key] is None else 'object' if isinstance(data[key],dict) else 'invalid'


def token_observation(data):
    """Context counters are observations, not yet validated billable usage."""
    session=data.get('conversation_id') or data.get('session_id')
    session=hashlib.sha256(session.encode()).hexdigest() if isinstance(session,str) and session else None
    model=data.get('model') or {}
    model=model.get('id') if isinstance(model,dict) else None
    if not isinstance(model,str) or not re.fullmatch(r'[a-zA-Z0-9_.:/ ()+-]{1,160}',model):model=None
    version=data.get('version')
    if not isinstance(version,str) or not re.fullmatch(r'[0-9a-zA-Z.+-]{1,40}',version):version=None
    context=data.get('context_window')
    current=context.get('current_usage') if isinstance(context,dict) else None
    return dict(session=session,model=model,version=version,context_state=field_state(data,'context_window'),
                current_state=field_state(context,'current_usage') if isinstance(context,dict) else 'missing',
                totals=token_fields(context,TOKEN_FIELDS),current=token_fields(current,CURRENT_FIELDS))


def validate_observation(value):
    """Reapply the allowlist on import; never trust an inbox's arbitrary JSON."""
    if not isinstance(value,dict):raise ValueError('invalid observation')
    session=value.get('session')
    if session is not None and (not isinstance(session,str) or not re.fullmatch('[0-9a-f]{64}',session)):
        raise ValueError('invalid identity')
    raw=dict(model={'id':value.get('model')},version=value.get('version'),context_window={})
    clean=token_observation(raw)
    clean['session']=session
    for key in ('context_state','current_state'):
        if value.get(key) not in ('missing','null','object','invalid'):raise ValueError('invalid state')
        clean[key]=value[key]
    clean['totals']=token_fields(value.get('totals'),TOKEN_FIELDS)
    clean['current']=token_fields(value.get('current'),CURRENT_FIELDS)
    return clean


def observation_key(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def record_tokens(directory,data,checked):
    value=token_observation(data)
    path=directory/'antigravity-tokens.db'
    # SQLite serializes concurrent TUI callbacks on Windows and WSL. No raw stdin.
    fd=os.open(path,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
    with closing(sqlite3.connect(path,timeout=1)) as c, c:
        c.execute('CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, first_seen REAL, last_seen REAL, data TEXT)')
        c.execute('INSERT INTO observations VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                  'first_seen=MIN(first_seen,excluded.first_seen),last_seen=MAX(last_seen,excluded.last_seen)',
                  (observation_key(value),checked,checked,json.dumps(value,sort_keys=True)))
        c.execute('DELETE FROM observations WHERE last_seen<?',(checked-30*86400,))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--route',choices=['claude-code','antigravity'],required=True)
    parser.add_argument('--directory',required=True)
    args=parser.parse_args()
    raw=sys.stdin.buffer.read()
    directory=Path(args.directory)
    try:
        data=json.loads(raw)
        safe=sanitize(args.route,data)
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,tmp=tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd,'w') as f:json.dump(dict(route=args.route,checked=time.time(),data=safe),f)
            os.replace(tmp,directory/(args.route+'.json'))
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    except Exception:
        pass  # A collector failure must never alter the coding tool's display.
    if args.route=='antigravity':
        try:record_tokens(directory,json.loads(raw),time.time())
        except Exception:pass  # Quota capture and the original display remain independent.
    try:command=json.loads((directory/(args.route+'-original.json')).read_text()).get('command')
    except (OSError,ValueError,AttributeError):command=None  # Broken wiring must not break the tool's display.
    code=subprocess.run(command,shell=True,input=raw).returncode if command else 0
    # Opt-in quota summary written by the collector; a missing or old file adds nothing.
    try:
        summary=directory/'summary.txt'
        if time.time()-summary.stat().st_mtime<=900:
            sys.stdout.flush();sys.stdout.write(summary.read_text().strip()[:300]+'\n');sys.stdout.flush()
    except (OSError,UnicodeDecodeError):pass
    return code


if __name__=='__main__':
    raise SystemExit(main())
