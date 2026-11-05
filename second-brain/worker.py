"""One process, one steward, resumable durable jobs. No server port required."""
import datetime as dt
import json
import re
import sys
import time
from api import API, APIError
from collector import inventory, capture_batch
from quality import receipt_from, verify, snapshot, text_of
from storage import ROOT, STATE, atomic_json, config, connect, digest, exclusive_lock, get, put, log
from focus import prepare, schema as focus_schema

def writer(db,api,cfg):
    sid=get(db,'writer')
    if sid:
        try:
            session=api.session(sid)
            if not session.get('time',{}).get('archived'):
                return session
        except APIError as error:
            if error.status!=404: raise
    for session in api.sessions():
        if session.get('title')==cfg['title']:
            with db: put(db,'writer',session['id'])
            return session
    session=api.call('/api/session',{
        'title':cfg['title'],'agent':'build','location':{'directory':cfg['vault']},
        'metadata':{'second_brain':{'role':'writer','version':3,'previous':get(db,'previous_writer')}}
    })['data']
    with db: put(db,'writer',session['id'])
    return session

def idle_for_manual(db,api,session):
    sid=session['id']
    pending=api.call(f'/api/session/{sid}/inbox')['data']
    if pending:
        return False
    messages,_=api.messages(sid)
    if not messages:
        return True
    latest_user=next((m for m in reversed(messages) if m.get('type')=='user'),None)
    latest_idle=next((m for m in reversed(messages) if m.get('type')=='idle'),None)
    if latest_user and (not latest_idle or latest_user['time']['created']>latest_idle['time']['created']):
        return False
    if latest_user and not latest_user.get('metadata',{}).get('second_brain'):
        return time.time()-latest_idle['time']['created']/1000>60
    return True

def rotate(db,api,cfg,session):
    messages,_=api.messages(session['id'])
    turns=[m for m in messages if m.get('type')=='user' or
           (m.get('type')=='assistant' and text_of(m).strip())]
    if len(turns)<cfg['rotate_messages']:
        return session
    # Archive first. No new visible entry is created if archive fails.
    api.update(session['id'],title=cfg['title']+' · '+dt.date.today().isoformat(),archived=True)
    if not api.session(session['id']).get('time',{}).get('archived'):
        raise RuntimeError('旧整理会话归档尚未生效')
    with db:
        put(db,'previous_writer',session['id']);put(db,'writer',None)
    return writer(db,api,cfg)

def candidates(db,api,cfg,source):
    all_models=api.call('/api/model')['data']
    usable=[m for m in all_models if m.get('enabled') and m.get('status')!='deprecated'
            and m.get('capabilities',{}).get('tools') and not m['id'].endswith('-free')]
    preferred=cfg['preferred_providers']
    def score(m):
        family=0 if re.search(cfg['preferred_family'],m['id'],re.I) else 1
        small=0 if re.search('flash|mini|lite|haiku|air',m['id'],re.I) else 1
        provider=preferred.index(m['providerID']) if m['providerID'] in preferred else 99
        version=[int(n) for n in re.findall(r'\d+',m['id'])]
        return (family,small,provider,[-v for v in version],m['id'])
    usable.sort(key=score)
    result=[]
    for model in usable:
        key=model['providerID']+'/'+model['id']
        state=db.execute('SELECT * FROM models WHERE key=?',(key,)).fetchone()
        if state and state['cooldown']>time.time():
            continue
        result.append({'providerID':model['providerID'],'id':model['id']})
    return result

def fail(db,job,error,cfg):
    attempts=job['attempts']
    delay=min(3600,60*2**attempts)
    with db:
        db.execute("UPDATE jobs SET status='failed',error=?,next_at=?,finished=? WHERE id=?",
                   (str(error)[:1500],time.time()+delay,time.time(),job['id']))
    log('job_failed',job=job['id'],attempts=attempts,error=str(error))

def complete(db,job,result):
    refs=json.loads(job['refs'])
    with db:
        db.execute('UPDATE jobs SET status=?,receipt=?,finished=?,error=NULL WHERE id=?',
                   (result['status'],json.dumps(result,ensure_ascii=False),time.time(),job['id']))
        if job['id'].startswith('focus_'):
            db.execute('UPDATE focus_ranges SET status=? WHERE job=?',(result['status'],job['id']))
            # A selected-window result never verifies the omitted source ranges.
            log('focus_verified',job=job['id'],status=result['status'])
            return
        for ref in refs:
            db.execute('INSERT OR REPLACE INTO verified_messages VALUES(?,?,?)',(ref['id'],ref['hash'],job['id']))
        # Acknowledgment advances only through the consecutive verified prefix.
        jobs=db.execute('SELECT * FROM jobs WHERE source=? ORDER BY position',(job['source'],)).fetchall()
        acknowledged=None
        for row in jobs:
            if row['status'] not in ('written_verified','skipped_verified'):
                break
            acknowledged=row['last_id']
        if acknowledged:
            db.execute('UPDATE sources SET acknowledged_id=? WHERE id=?',(acknowledged,job['source']))
    log('job_verified',job=job['id'],status=result['status'],paths=[n['path'] for n in result.get('checks',[])])

def poll_job(db,api,cfg,job):
    messages,_=api.messages(job['writer'])
    start=next((i for i,m in enumerate(messages) if m['id']==job['prompt_id']),None)
    if start is None:
        if time.time()-job['started']>60:
            fail(db,job,'已提交提示未在历史中出现，保留任务待重试',cfg)
        return
    own=[]
    for message in messages[start+1:]:
        if message.get('type')=='user':
            # Manual interaction cannot count as this job's result.
            break
        own.append(message)
    receipt=receipt_from(own,job['id'])
    ended=next((m for m in own if m.get('type')=='idle'),None)
    if receipt and ended:
        try:
            before=get(db,'snapshot:'+job['id'],{})
            result=verify(job,receipt,own,cfg,before)
            complete(db,job,result)
            model=json.loads(job['model']);key=model['providerID']+'/'+model['id']
            with db:
                db.execute('''INSERT INTO models(key,successes) VALUES(?,1)
                    ON CONFLICT(key) DO UPDATE SET successes=successes+1,cooldown=0''',(key,))
        except Exception as error:
            fail(db,job,'核验失败：'+str(error),cfg)
        return
    if ended or time.time()-job['started']>cfg['job_timeout_seconds']:
        error='任务结束但缺少有效专属回执' if ended else '任务超时；保留任务等待空闲后再试'
        if not ended:
            latest_user=next((m for m in reversed(messages) if m.get('type')=='user'),None)
            pending=api.call(f"/api/session/{job['writer']}/inbox")['data']
            if latest_user and latest_user['id']==job['prompt_id'] and not pending:
                api.call(f"/api/session/{job['writer']}/interrupt",method='POST',query={'resume':'false'})
                error+='；仅中断本任务超时调用'
        if ended and ended.get('error'):
            error=str(ended['error'])
        fail(db,job,error,cfg)
        # Tool failures and missing receipts do not establish model unavailability.
        if re.search('model|provider|unauthorized|api.?key|rate.?limit|401|429',error,re.I):
            model=json.loads(job['model']);key=model['providerID']+'/'+model['id']
            with db:
                db.execute('''INSERT INTO models(key,cooldown,failures,error) VALUES(?,?,1,?)
                  ON CONFLICT(key) DO UPDATE SET cooldown=excluded.cooldown,failures=failures+1,error=excluded.error''',
                  (key,time.time()+1800,error[:1000]))

def dispatch(db,api,cfg):
    processing=db.execute("SELECT * FROM jobs WHERE status='processing' ORDER BY started LIMIT 1").fetchone()
    if processing:
        poll_job(db,api,cfg,processing)
        return
    if not cfg['dispatch']:
        return
    day=time.time()-86400
    usage=[stamp for stamp in get(db,'admissions',[]) if stamp>day]
    calls=len(usage)
    if calls>=cfg['daily_job_limit']:
        return
    if cfg.get('processing_mode')=='focus':
        prepare(db,cfg,cfg.get('focus_batch_sources',4))
        job=db.execute("""SELECT j.* FROM jobs j JOIN sources s ON s.id=j.source
          WHERE j.id LIKE 'focus_%' AND j.status IN ('pending','failed') AND j.attempts<?
            AND j.next_at<? AND s.status!='excluded'
          ORDER BY j.created LIMIT 1""",(cfg['max_attempts'],time.time())).fetchone()
        if job:
            excluded=db.execute("""SELECT 1 FROM focus_ranges f JOIN sources s ON s.id=f.source
                 WHERE f.job=? AND s.status='excluded'""",(job['id'],)).fetchone()
            if excluded:
                with db:
                    db.execute("UPDATE jobs SET status='excluded',error='批次来源已排除，其他来源重新建窗' WHERE id=?",(job['id'],))
                    db.execute('DELETE FROM focus_ranges WHERE job=?',(job['id'],))
                return
    else:
        job=legacy_job(db,cfg)
    if not job:
        return
    submit(db,api,cfg,job,usage)

def legacy_job(db,cfg):
    job=db.execute('''SELECT j.* FROM jobs j JOIN sources s ON s.id=j.source
      WHERE j.status IN ('pending','failed') AND j.attempts<? AND j.next_at<? AND s.status!='excluded'
      AND (?=0 OR (s.id IN (SELECT value FROM json_each(?)) AND NOT EXISTS
          (SELECT 1 FROM jobs done WHERE done.source=j.source AND done.status IN ('written_verified','skipped_verified'))))
      AND NOT EXISTS(SELECT 1 FROM jobs p WHERE p.source=j.source AND p.position<j.position
                     AND p.status NOT IN ('written_verified','skipped_verified'))
       ORDER BY CASE WHEN EXISTS(SELECT 1 FROM messages m WHERE m.source=j.source
                  AND m.position=j.position AND m.created>?) THEN 0 ELSE 1 END,
         CASE WHEN EXISTS(SELECT 1 FROM messages m WHERE m.source=j.source
                  AND m.position=j.position AND m.created>?) THEN 0 ELSE 1 END,j.created LIMIT 1''',
       (cfg['max_attempts'],time.time(),int(cfg.get('validation_only',False)),
         json.dumps(cfg.get('validation_sessions',[])),(time.time()-86400)*1000,
         (time.time()-30*86400)*1000)).fetchone()
    return job

def submit(db,api,cfg,job,usage):
    refs=json.loads(job['refs'])
    if not job['id'].startswith('focus_') and all(db.execute('SELECT 1 FROM verified_messages WHERE id=? AND hash=?',(r['id'],r['hash'])).fetchone() for r in refs):
        complete(db,job,{'status':'skipped_verified','reason':'fork共享消息ID和指纹已核验','checks':[]})
        return
    session=writer(db,api,cfg)
    if not idle_for_manual(db,api,session):
        return
    session=rotate(db,api,cfg,session)
    # Optional: give this vault directory its own stdio connection when a shared
    # HTTP proxy is unstable. Disabled unless config.json sets "mcp_command".
    command=cfg.get('mcp_command')
    if command:
        command=[str(part).replace('{vault}',cfg['vault']) for part in command]
        server=api.call('/api/info')
        server_key=json.dumps(server,sort_keys=True)
        if get(db,'mcp_server')!=server_key:
            api.call('/api/experimental/mcp/'+cfg.get('mcp_server','obsidian'),{
                'config':{'type':'local','command':command,
                          'timeout':{'startup':60000,'execution':45000}}},method='PUT',
                query={'location[directory]':cfg['vault']},timeout=70)
            with db: put(db,'mcp_server',server_key)
    source=db.execute('SELECT * FROM sources WHERE id=?',(job['source'],)).fetchone()
    models=candidates(db,api,cfg,source)
    if not models:
        raise RuntimeError('模型目录无兼容工具模型，等待配置或认证恢复')
    model=models[0]
    try:
        api.call(f"/api/session/{session['id']}/model",{'model':model})
    except APIError as error:
        key=model['providerID']+'/'+model['id']
        with db:
            db.execute('''INSERT INTO models(key,cooldown,failures,error) VALUES(?,?,1,?)
              ON CONFLICT(key) DO UPDATE SET cooldown=excluded.cooldown,failures=failures+1,error=excluded.error''',
              (key,time.time()+1800,str(error)))
        raise
    prompt=(ROOT/('focus-prompt.txt' if job['id'].startswith('focus_') else 'prompt.txt')).read_text(encoding='utf-8')
    prompt=prompt.replace('{vault}',cfg['vault'])
    if job['error']:
        prompt+='\n上一尝试未通过核验：'+job['error']+'。先检查上次已写内容，修正缺项；已存在且正确的事实按重复处理，避免重复创建。\n'
    previous=get(db,'previous_writer')
    if previous:
        prompt+=f'\n交接：上一整理会话 {previous} 已归档。长期事实以笔记为准，处理进度以台账为准，不重做旧任务。\n'
    context=db.execute('SELECT kind,text FROM messages WHERE source=? AND position<? ORDER BY position DESC LIMIT 2',
                       (job['source'],job['position']-len(refs)+1)).fetchall()
    preceding='' if job['id'].startswith('focus_') else '\n'.join(r['text'][-1200:] for r in reversed(context))
    prompt+=f"\n本轮ID：{job['id']}\n来源会话：{job['source']}\n标题：{source['title']}\n目录：{source['directory']}\n"
    prompt+='来源消息ID：'+json.dumps([r['id'] for r in refs],ensure_ascii=False)+'\n'
    prompt+='必要前文（仅背景）：\n'+preceding+'\n本轮提供的证据窗口：\n'+job['evidence']
    message_id='msg_'+digest(job['id']+':'+str(job['attempts']))[:26]
    before=snapshot(cfg['vault'])
    with db:
        put(db,'admissions',usage+[time.time()])
        put(db,'snapshot:'+job['id'],before)
        db.execute("UPDATE jobs SET status='processing',attempts=attempts+1,started=?,writer=?,prompt_id=?,model=? WHERE id=?",
                   (time.time(),session['id'],message_id,json.dumps(model),job['id']))
    # Deterministic input ID prevents duplicate prompt admission after uncertain network completion.
    try:
        api.prompt(session['id'],prompt,message_id,{'second_brain':{'job':job['id']}})
        log('job_dispatched',job=job['id'],writer=session['id'],model=model)
    except Exception as error:
        log('admission_uncertain',job=job['id'],error=str(error))

def dashboard(db,cfg,error=None):
    focus_schema(db)
    counts={r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM jobs GROUP BY status')}
    sources={r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM sources GROUP BY status')}
    heartbeat={'time':time.time(),'pid':__import__('os').getpid(),'enabled':cfg['enabled'],
               'dispatch':cfg['dispatch'],'validation_only':cfg['validation_only'],
                'writer':get(db,'writer'),'jobs':counts,'sources':sources,'error':error}
    heartbeat['processing_mode']=cfg.get('processing_mode','legacy')
    if cfg.get('processing_mode')=='focus':
        heartbeat['focus_jobs']={r['status']:r['n'] for r in db.execute("SELECT status,COUNT(*) n FROM jobs WHERE id LIKE 'focus_%' GROUP BY status")}
    atomic_json(STATE/'health.json',heartbeat)
    if cfg.get('processing_mode')=='focus':
        dashboard_focus(db,cfg,heartbeat,error)
        return
    lines=['# 第二大脑处理进度','',f"更新时间：{dt.datetime.now().isoformat(timespec='seconds')}",
           '',f"自动执行：{'开启' if cfg['dispatch'] else '暂停'}；范围：{'小批验收' if cfg['validation_only'] else '全历史与新增'}。",
           '','这是台账生成的核验进度，任务结束不等于写入成功。','',
            '| 状态 | 消息段数 |','| --- | ---: |']
    labels={'pending':'待处理','processing':'正在处理','written_verified':'已核验写入',
            'skipped_verified':'已核验跳过','failed':'失败待处理','excluded':'已排除'}
    for key in ('pending','processing','written_verified','skipped_verified','failed','excluded'):
        lines.append(f'| {labels[key]} | {counts.get(key,0)} |')
    lines+=['',f'已发现会话 {sum(sources.values())}；排除 {sources.get("excluded",0)}。',
            '完整会话数量不等于有价值笔记数量；未扫描、失败和无权限都不算整理完成。',
            '', '## 最近处理结果','']
    for row in db.execute('SELECT j.*,s.title FROM jobs j JOIN sources s ON s.id=j.source ORDER BY COALESCE(finished,started,created) DESC LIMIT 20'):
        receipt=json.loads(row['receipt']) if row['receipt'] else {}
        paths=' '.join('[['+n['path'].removesuffix('.md')+']]' for n in receipt.get('checks',[]))
        lines.append(f"- {labels.get(row['status'],row['status'])} · {row['title']} · `{row['id']}` {paths}")
        if row['error']:
            lines.append('  - 阻塞：'+row['error'].replace('\n',' ')[:250])
    if error:
        lines+=['','运行阻塞：'+str(error)[:500]]
    lines+=['','来源：独立 SQLite 台账自动生成。暂停/排除/重试使用本机 second-brain/config.json，台账不存原始凭据。']
    target=__import__('pathlib').Path(cfg['vault'])/'01-看板/第二大脑处理进度.md'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text('\n'.join(lines)+'\n',encoding='utf-8')

def dashboard_focus(db,cfg,health,error):
    unscanned=db.execute("SELECT COUNT(*) FROM sources WHERE status!='excluded' AND scanned_at=0").fetchone()[0]
    counts={r['status']:r['n'] for r in db.execute("SELECT status,COUNT(*) n FROM jobs WHERE id LIKE 'focus_%' GROUP BY status")}
    coverage=db.execute('SELECT COUNT(DISTINCT source),COALESCE(SUM(selected_chars),0),COALESCE(SUM(original_chars),0) FROM focus_ranges').fetchone()
    pending=db.execute("""SELECT COUNT(*) FROM sources s WHERE status!='excluded' AND EXISTS(
       SELECT 1 FROM messages m WHERE m.source=s.id AND m.position>COALESCE(
       (SELECT MAX(last_position) FROM focus_ranges f WHERE f.source=s.id),0))""").fetchone()[0]
    usage=sorted(t for t in get(db,'admissions',[]) if t>time.time()-86400)
    average=db.execute("SELECT AVG(finished-started) FROM jobs WHERE id LIKE 'focus_%' AND finished>started AND started>0 AND status IN ('written_verified','skipped_verified','focus_screened')").fetchone()[0]
    sampled=db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'focus_sampling:%'").fetchone()[0]
    lines=['# 第二大脑处理进度','',f"更新时间：{dt.datetime.now().isoformat(timespec='seconds')}",
      '',f"模式：重要记忆优先；派发：{'开启' if cfg['dispatch'] else '暂停'}。不追求全部原文精读。",
      f"整理会话：`{health['writer']}`",'',f'已建立重点窗口：{coverage[0]} 个会话；已采集待选会话：{pending}。',
      f'选取正文：{coverage[1]:,} 字符；对应原证据：{coverage[2]:,} 字符。未选原文保留可回查。',
       f"尚未首次扫描：{unscanned} 个会话；{sampled} 个会话采用开头/近况抽样，中间原文未精读。",
       '',f"过去24小时模型提交：{len(usage)}/{cfg['daily_job_limit']}（包括旧任务、重试）。"]
    lines.append(f"日预算剩余：{max(0,cfg['daily_job_limit']-len(usage))}；最近重点批次平均耗时：{round(average)} 秒。" if average else '重点批次耗时尚无完成样本；不提供未经实测的完成时间。')
    if len(usage)>=cfg['daily_job_limit'] and usage:
        lines.append('预算最早释放：'+dt.datetime.fromtimestamp(usage[0]+86400).isoformat(timespec='seconds'))
    labels={'pending':'待筛选','processing':'筛选整理中','written_verified':'有实际核验写入的批次',
       'skipped_verified':'现有笔记已核验的批次','focus_screened':'重点筛选未提取（并非完整审阅）','failed':'失败保留'}
    lines+=['','| 重点批次 | 数量 |','| --- | ---: |']
    lines += [f'| {label} | {counts.get(key,0)} |' for key,label in labels.items()]
    legacy=db.execute("SELECT COUNT(*) FROM jobs WHERE status='superseded'").fetchone()[0]
    lines+=['',f'旧细碎任务已由重点窗口接管：{legacy}；未接管任务仍保留，不计入新模式必做数量。','', '## 最近重点结果','']
    for row in db.execute("SELECT * FROM jobs WHERE id LIKE 'focus_%' ORDER BY COALESCE(finished,started,created) DESC LIMIT 12"):
        result=json.loads(row['receipt'] or '{}')
        paths=' '.join('[['+n['path'].removesuffix('.md')+']]' for n in result.get('checks',[]))
        lines.append(f"- {labels.get(row['status'],row['status'])} · `{row['id']}` {paths}")
        titles=[r[0] for r in db.execute('SELECT s.title FROM focus_ranges f JOIN sources s ON s.id=f.source WHERE f.job=?',(row['id'],))]
        lines.append('  - 主题：'+'；'.join(titles))
        if result.get('reason'):lines.append('  - 摘取结果：'+str(result['reason'])[:350])
        if row['error']:lines.append('  - '+row['error'][:250])
    if error:lines+=['','运行阻塞：'+str(error)[:500]]
    target=__import__('pathlib').Path(cfg['vault'])/'01-看板/第二大脑处理进度.md'
    target.write_text('\n'.join(lines)+'\n',encoding='utf-8')

def main():
    with exclusive_lock() as owned:
        if not owned:
            return
        db=connect();api=API()
        log('worker_started',pid=__import__('os').getpid())
        once='--once' in sys.argv
        while True:
            cfg=config();error=None
            try:
                if cfg['enabled']:
                    if time.time()-get(db,'inventory_at',0)>120:
                        inventory(db,api,cfg)
                    dispatch(db,api,cfg)
                    capture_batch(db,api,cfg)
            except Exception as exc:
                error=str(exc);log('cycle_failed',error=error)
            try:
                dashboard(db,cfg,error)
            except Exception as exc:
                log('dashboard_failed',error=str(exc))
            if once:
                break
            time.sleep(cfg['poll_seconds'])

if __name__=='__main__':
    main()
