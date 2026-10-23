"""One numeric usage interpretation for local and cloud Claude transcripts."""
from .store import identity, project_label, stamp


def counter(value):
    if value is None:return 0
    if type(value) is not int or not 0<=value<=2**63-1:raise ValueError('invalid usage counter')
    return value


def components(usage):
    if not isinstance(usage,dict):raise ValueError('invalid usage object')
    values={name:counter(usage.get(field)) for name,field in (
        ('uncached_input','input_tokens'),('cached_input','cache_read_input_tokens'),
        ('output','output_tokens'),('cache_creation','cache_creation_input_tokens'))}
    output=usage.get('output_tokens_details') or {}
    if not isinstance(output,dict):raise ValueError('invalid output details')
    reasoning=output.get('thinking_tokens',usage.get('reasoning_output_tokens'))
    try:
        values.update(reasoning=counter(reasoning),reasoning_observed=int(reasoning is not None))
        if values['reasoning']>values['output']:raise ValueError('thinking exceeds output')
    except ValueError:
        values.update(reasoning=0,reasoning_observed=0,_reasoning_error=True)
    ttl=usage.get('cache_creation') or {}
    if not isinstance(ttl,dict):raise ValueError('invalid cache details')
    five,hour=(ttl.get(k) for k in ('ephemeral_5m_input_tokens','ephemeral_1h_input_tokens'))
    if five is not None and hour is not None:
        try:
            five,hour=counter(five),counter(hour)
            if five+hour!=values['cache_creation']:raise ValueError('cache detail total differs')
            values.update(cache_creation_5m=five,cache_creation_1h=hour,cache_ttl_observed=1)
        except ValueError:
            values.update(cache_creation_unknown=values['cache_creation'],_cache_error=True)
    else:values['cache_creation_unknown']=values['cache_creation']
    return values


def detail_status(store,c,key,values):
    for field,observed in (('cache','cache_ttl_observed'),('reasoning','reasoning_observed')):
        record=key+':'+field
        if values.get('_'+field+'_error'):store.import_error(c,'claude-details',record,field+'ComponentMismatch')
        elif values.get(observed):c.execute("DELETE FROM import_errors WHERE source='claude-details' AND record=?",(record,))


def summary(store,c):
    bad=c.execute("SELECT COUNT(*) FROM import_errors WHERE source='claude-details'").fetchone()[0]
    conflict=c.execute("SELECT COALESCE(SUM(cache_ttl_conflict),0) FROM events WHERE route='claude-code'").fetchone()[0]
    if bad or conflict:
        store.source_changed(c,'claude-details','partial',f'원본 세부값 불일치 {bad}건 · 캐시 관측 상충 {conflict}건 · 전체 사용량은 보존하고 해당 세부값은 미확인으로 표시합니다.')
    elif c.execute("SELECT 1 FROM sources WHERE name='claude-details'").fetchone():
        store.source_changed(c,'claude-details','ok','현재 확인된 세부값 불일치가 없습니다.')


def ingest_message(store,c,route,data,*,session=None,project=None,account=None,source='local',finality='final'):
    from .collect import model_provider
    from .reconciliation import aliases,link
    message=data.get('message') or {};usage=message.get('usage');ts=stamp(data.get('timestamp'))
    if not usage or not message.get('id') or not ts:return 0
    key=identity(route,message['id']);sid=session or data.get('sessionId') or data.get('session_id')
    project=project if project is not None else project_label(data.get('cwd'))
    kind='sub' if data.get('isSidechain') or data.get('parent_tool_use_id') else 'main'
    iterations=usage.get('iterations') or []
    if not isinstance(iterations,list):raise ValueError('invalid usage iterations')
    advisors=[(i,part) for i,part in enumerate(iterations) if isinstance(part,dict) and part.get('type')=='advisor_message']
    values=components(usage);detail_status(store,c,key,values)
    store.event(c,key,ts,model_provider(message.get('model','')),route,message.get('model'),values,
                session=sid,project=project,agent_kind=kind,account=account,allow_empty=bool(advisors))
    aliases(c,route,key,message['id'],data.get('requestId'),data.get('request_id'),data.get('uuid'))
    if route=='claude-code' and sid:link(c,'claude:'+sid,key,source,finality)
    # A historical parent may already have an account even when this replay does not.
    parent=c.execute('SELECT account FROM events WHERE id=?',(key,)).fetchone()
    for index,part in advisors:
        child=identity('claude-advisor',route,message['id'],index)
        values=components(part);values['advisor_calls']=1
        detail_status(store,c,child,values)
        store.event(c,child,ts,model_provider(part.get('model','')),route,part.get('model'),values,
                    session=sid,project=project,agent_kind=kind,account=parent['account'] if parent else account,
                    parent_id=key,requests=0,allow_empty=True)
        if route=='claude-code' and sid:link(c,'claude:'+sid,child,source,finality)
    return 1
