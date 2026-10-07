"""Derived observations, never account-wide coverage or inferred quota resets."""
from datetime import datetime

from .store import KST, TOKENS


TOTAL_SQL='uncached_input+cached_input+output+cache_creation'


def token_total(values):
    return sum(values.get(key,0) for key in TOKENS if key!='reasoning')


def cache_rate(values):
    denominator=sum(values.get(key,0) for key in ('uncached_input','cached_input','cache_creation'))
    return 100*values.get('cached_input',0)/denominator if denominator else None


def usage_insights(c,a,b,now,period,first,totals,rows,buckets,scope_sql='',scope_args=()):
    quality=dict(c.execute('''SELECT COUNT(*) AS sessions,
      COALESCE(SUM(resets>0),0) AS reset_sessions,COALESCE(SUM(partial),0) AS partial_sessions,
      COALESCE(SUM(ambiguous>0),0) AS ambiguous_sessions,COALESCE(SUM(zero_baselines>0),0) AS zero_baseline_sessions
      FROM session_quality''').fetchone())
    quality['sessions_observed']=c.execute('SELECT COUNT(DISTINCT session) FROM codex_points').fetchone()[0]
    quality['import_errors']=c.execute('SELECT COUNT(*) FROM import_errors').fetchone()[0]
    quality['last_successes']=[dict(row) for row in c.execute('SELECT * FROM source_health ORDER BY name')]
    quality['unknown_model_tokens']=sum(token_total(row) for row in rows if row['model']=='unknown')
    quality['unknown_model_percent']=100*quality['unknown_model_tokens']/token_total(totals) if token_total(totals) else None
    quality['scope']='세션 진단은 전체 Codex 이력, 해석 실패·마지막 성공은 기능 적용 후 확인한 기록, 모델 미확인 비중은 선택 기간 기준입니다.'

    cache=dict(rate=cache_rate(totals),rows=[dict(provider=row['provider'],route=row['route'],model=row['model'],
          rate=cache_rate(row)) for row in rows],
          series=[dict(time=datetime.fromtimestamp(row['bucket'],KST).isoformat(),label=row['label'],rate=cache_rate(row)) for row in buckets])

    comparison=dict(status='unavailable',reason='전체 기간에는 이전 비교 기간을 정의하지 않습니다.')
    effective_end=min(b,now)
    if period!='all' and effective_end>a:
        previous_start=a-(b-a)
        previous_end=previous_start+(effective_end-a)
        def grouped(start,end):
            return {(r['provider'],r['route'],r['model']):r['tokens'] for r in c.execute(
                'SELECT provider,route,model,SUM('+TOTAL_SQL+') AS tokens FROM events WHERE ts>=? AND ts<?'+scope_sql+' GROUP BY provider,route,model',
                (start.timestamp(),end.timestamp(),*scope_args))}
        current=grouped(a,effective_end);previous=grouped(previous_start,previous_end)
        curr=sum(current.values());prev=sum(previous.values())
        enough=first is not None and first<=previous_start.timestamp()
        contributions=[dict(provider=key[0],route=key[1],model=key[2],current=current.get(key,0),previous=previous.get(key,0),
                       delta=current.get(key,0)-previous.get(key,0)) for key in current.keys()|previous.keys()]
        contributions.sort(key=lambda r:(-abs(r['delta']),r['route'],r['model']))
        comparison=dict(status='observed' if enough else 'insufficient_history',
            reason='확보한 로컬 기록끼리 비교합니다. 누락된 원격 사용은 포함되지 않습니다.' if enough else '최초 기록이 이전 기간 시작보다 늦어 증감률을 계산하지 않습니다.',
            start=a.isoformat(),end_exclusive=effective_end.isoformat(),previous_start=previous_start.isoformat(),
            previous_end_exclusive=previous_end.isoformat(),current=curr,previous=prev,delta=curr-prev,
            percent=(curr-prev)/prev*100 if enough and prev else None,contributions=contributions,
            elapsed_seconds=(effective_end-a).total_seconds())
    elif period!='all':
        comparison['reason']='아직 시작하지 않은 기간은 비교하지 않습니다.'
    return dict(quality=quality,cache=cache,comparison=comparison)


def quota_history(c,now,window=86400):
    # At most one original observation per five-minute/source/reset group.
    # SQLite's single MAX selects the other columns from that original row.
    # The UI draws 24 hours; trends (1h), decreases (12h) and pace fit inside it.
    rows=c.execute('''SELECT route,bucket,MAX(checked) AS checked,remaining,resets,source
      FROM limit_history WHERE checked>=? AND checked<=?
      GROUP BY route,bucket,CAST(checked/300 AS INTEGER),source,resets ORDER BY checked''',(now-window,now)).fetchall()
    interruptions={}
    for row in c.execute('SELECT * FROM quota_interruptions WHERE checked>=?',(now-window,)):
        interruptions.setdefault(row['source'],[]).append(row['checked'])
    streams={}
    for row in rows:streams.setdefault((row['route'],row['bucket']),[]).append(dict(row))
    result={}
    for key,observations in streams.items():
        # Account polling and local notifications are separate observation streams.
        # Prefer recent account polling, without alternating between their samples.
        latest=observations[-1]['checked']
        account_source='claude-oauth' if key[0]=='claude-code' else key[0]
        preferred=[p for p in observations if p['source']==account_source and latest-p['checked']<=600]
        source=preferred[-1]['source'] if preferred else observations[-1]['source']
        points=[];epoch_reset=None
        for point in (p for p in observations if p['source']==source):
            point['break_before']=True;point['break_reason']='start'
            if points:
                previous=points[-1]
                # The same original reset can jitter by 1s between responses. Compare
                # with the epoch anchor so small changes cannot accumulate into drift.
                reason=('gap' if point['checked']-previous['checked']>600 else
                    'unknown' if point['resets'] is None or epoch_reset is None or point['remaining'] is None or previous['remaining'] is None else
                    'reset' if abs(point['resets']-epoch_reset)>2 else
                    'expired' if point['checked']>=min(point['resets'],epoch_reset) else
                    'increase' if point['remaining']>previous['remaining'] else
                    'error' if any(previous['checked']<checked<=point['checked'] for checked in interruptions.get(source,[])) else None)
                point['break_before']=reason is not None;point['break_reason']=reason
            if point['break_before']:epoch_reset=point['resets']
            if points and int(point['checked']/300)==int(points[-1]['checked']/300) and not point['break_before']:
                point['break_before']=points[-1]['break_before'];point['break_reason']=points[-1]['break_reason']
                points[-1]=point
            else:points.append(point)
        result[key]=points
    return result


WINDOW_SECONDS={'five_hour':18000,'rolling':18000,'daily':86400,'weekly':604800,'monthly':2592000}


def window_seconds(bucket):
    """Length of the provider's quota window, parsed only from the original bucket name."""
    name=(bucket or '').lower()
    if ' · ' in name:
        minutes=name.rsplit(' · ',1)[-1].removesuffix('분')
        return int(minutes)*60 if minutes.isdigit() else None
    if name in WINDOW_SECONDS:return WINDOW_SECONDS[name]
    if '5h' in name:return 18000
    if 'weekly' in name or name.startswith('seven_day'):return 604800
    return None


# Windows longer than a day are judged on a longer trailing pace: a 15-minute burst
# extrapolated over several days raised false "depletes before reset" alerts.
LONG_WINDOW=86400


def pace_lookback(bucket):
    """(trailing seconds, minimum observed minutes) for a bucket's pace and forecast."""
    span=window_seconds(bucket)
    return (3*3600,90) if span and span>LONG_WINDOW else (3600,15)


def quota_forecast(row,now):
    """Projected depletion from the observed pace; never invents a reset."""
    pace=row.get('pace_per_hour');remaining=row.get('remaining');resets=row.get('resets')
    minutes=row.get('pace_minutes')
    if row.get('status')!='fresh' or pace is None or remaining is None or not resets or resets<=now:return None
    if minutes is None or minutes<pace_lookback(row.get('bucket'))[1]:return None
    seconds_to_deplete=remaining/pace*3600 if pace>0 else None
    projected=remaining-pace*(resets-now)/3600
    return dict(per_hour=pace,observed_minutes=minutes,
                depletes_at=now+seconds_to_deplete if seconds_to_deplete is not None else None,
                within_window=bool(seconds_to_deplete is not None and now+seconds_to_deplete<resets),
                projected_remaining=max(0,projected))


def quota_plan(row,now):
    """Even-pace comparison inside the current window, from its original reset and
    the window length in the bucket name. Positive 'ahead' means more remains than
    an even spend would leave; nothing is returned without both values."""
    span=window_seconds(row.get('bucket'));resets=row.get('resets');remaining=row.get('remaining')
    if row.get('status')!='fresh' or not span or not resets or remaining is None or resets<=now:return None
    elapsed=now-(resets-span)
    if not 0<=elapsed<=span:return None
    expected=100*(1-elapsed/span)
    return dict(elapsed_fraction=elapsed/span,expected_remaining=expected,ahead=remaining-expected,window_start=resets-span)


def quota_pace(history,now,status,lookback=3600):
    """(percent/hour, observed minutes) over the trailing lookback of the last
    uninterrupted segment. Averages away single-sample quantization noise, so
    the caller can require enough observation before forecasting."""
    if status!='fresh':return None
    segment=[]
    for point in history:
        if point.get('break_before'):segment=[]
        segment.append(point)
    if len(segment)<2 or now-segment[-1]['checked']>600:return None
    end=segment[-1]
    if end['resets'] is None or end['resets']<=now:return None
    points=[p for p in segment if p['checked']>=end['checked']-lookback]
    if len(points)<2:return None
    start=points[0];seconds=end['checked']-start['checked']
    if seconds<60 or start['remaining'] is None or end['remaining'] is None:return None
    return ((start['remaining']-end['remaining'])*3600/seconds,seconds/60)


def quota_trends(history,now,status):
    result=[]
    segment=[]
    for point in history:
        if point.get('break_before'):segment=[]
        segment.append(point)
    for minutes in (30,60):
        metric=dict(window_minutes=minutes,available=False,reason='관측 부족')
        if status!='fresh':metric['reason']='최신 한도 수신 대기'
        elif segment and now-segment[-1]['checked']<=600:
            end=segment[-1]
            points=[p for p in segment if p['checked']>=end['checked']-minutes*60]
            if len(points)>1:
                start=points[0];seconds=end['checked']-start['checked']
                if seconds>=60 and start['remaining'] is not None and end['remaining'] is not None and end['resets'] and end['resets']>now:
                    decrease=start['remaining']-end['remaining']
                    metric.update(available=True,start=start['checked'],end=end['checked'],observed_minutes=seconds/60,
                        start_remaining=start['remaining'],end_remaining=end['remaining'],decrease_pp=decrease,per_hour=decrease*3600/seconds)
        result.append(metric)
    return result


def quota_decreases(history,now):
    end_hour=int(now//3600)*3600
    hours={t:dict(time=t,decrease_pp=None,observed_minutes=0,intervals=0) for t in range(end_hour-11*3600,end_hour+1,3600)}
    for before,after in zip(history,history[1:]):
        t=int(after['checked']//3600)*3600
        if t not in hours or after.get('break_before') or after['checked']>now:continue
        seconds=after['checked']-before['checked']
        if not 0<seconds<=600 or before['remaining'] is None or after['remaining'] is None:continue
        delta=before['remaining']-after['remaining']
        if delta<0:continue
        row=hours[t];row['decrease_pp']=(row['decrease_pp'] or 0)+delta
        row['observed_minutes']+=seconds/60;row['intervals']+=1
    return list(hours.values())
