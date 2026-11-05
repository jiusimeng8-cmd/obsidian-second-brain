"""Resumable capture and deterministic range construction."""
import json
import re
import time
from api import public_text
from storage import digest, get, put, log

def is_writer(session):
    return (session.get('metadata',{}).get('second_brain',{}).get('role') in ('writer','validation')
            or session.get('title','').startswith(('OBS 记忆整理','[Obsidian memory]','zz ')))

def inventory(db, api, cfg):
    count = 0
    for archived in (False, True):
        for session in api.sessions(archived):
            sid = session['id']
            excluded = is_writer(session) or sid in cfg['exclude_sessions'] or sid in get(db,'private_sources',[])
            status = 'excluded' if excluded else 'pending'
            info = json.dumps(session, ensure_ascii=False)
            with db:
                db.execute('''INSERT INTO sources(id,title,directory,archived,updated,status,info)
                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                  title=excluded.title,directory=excluded.directory,archived=excluded.archived,
                  updated=excluded.updated,info=excluded.info,
                  status=CASE WHEN excluded.status='excluded' THEN 'excluded'
                    WHEN sources.status='excluded' THEN 'pending' ELSE sources.status END''',
                  (sid, session.get('title',''), session.get('location',{}).get('directory',''),
                   int(archived),session.get('time',{}).get('updated',0),status,info))
            count += 1
    with db:
        put(db,'inventory_at',time.time()); put(db,'inventory_count',count)
    return count

def make_jobs(db, source, cfg):
    if cfg.get('processing_mode')=='focus':
        return
    last = db.execute('SELECT MAX(position) FROM jobs WHERE source=?',(source,)).fetchone()[0] or 0
    rows = db.execute('SELECT * FROM messages WHERE source=? AND position>? ORDER BY position', (source,last)).fetchall()
    if not rows:
        return
    fragments, refs, size = [], [], 0
    def flush():
        nonlocal fragments, refs, size
        if not refs:
            return
        key = 'brain_' + digest(source + '|' + '|'.join(r['id'] for r in refs))[:24]
        db.execute('''INSERT OR IGNORE INTO jobs(id,source,first_id,last_id,position,evidence,refs,created)
                    VALUES(?,?,?,?,?,?,?,?)''', (key,source,refs[0]['id'],refs[-1]['id'],refs[-1]['position'],
                    '\n\n'.join(fragments),json.dumps(refs,ensure_ascii=False),time.time()))
        fragments,refs,size = [],[],0
    for row in rows:
        text = row['text']
        header = f"[{row['kind']} {row['id']} 时间毫秒={int(row['created'])}]\n"
        if refs and size + len(text) + len(header) > cfg['chunk_chars']:
            flush()
        fragments.append(header+text)
        refs.append({'id':row['id'],'hash':row['hash'],'position':row['position']})
        size += len(text)+len(header)
    flush()

def capture(db, api, cfg, source):
    sid = source['id']
    sampled=False
    if cfg.get('processing_mode')=='focus' and hasattr(api,'focus_messages'):
        messages,found,sampled=api.focus_messages(sid,source['captured_id'])
        if sampled:
            with db:
                put(db,'focus_sampling:'+sid,{'sampled_at':time.time(),
                    'policy':'开头30条与最新200条；中间未完整读取，可回查',
                    'after':source['captured_id']})
    else:
        messages, found = api.messages(sid,source['captured_id'])
    if source['captured_id'] and not found and not sampled:
        log('cursor_missing_full_reconcile',source=sid)
    now = time.time()
    # Freeze only fully idle turns; never submit a partial running assistant turn.
    last_idle = max((i for i,m in enumerate(messages) if m.get('type')=='idle' or
                    (m.get('type')=='assistant' and m.get('time',{}).get('completed') and
                     m.get('finish') in ('stop','length','error'))), default=-1)
    if last_idle < 0:
        with db:
            db.execute('UPDATE sources SET scanned_update=?,scanned_at=?,error=? WHERE id=?',
                       (source['updated'],now,'尚无已结束回合；保留等待后续消息',sid))
        return False
    messages = messages[:last_idle+1]
    if messages[-1]['time'].get('completed',messages[-1]['time']['created'])/1000 > now-cfg['quiet_seconds']:
        with db: db.execute('UPDATE sources SET scanned_at=? WHERE id=?',(now,sid))
        return False
    position = db.execute('SELECT COALESCE(MAX(position),0) FROM messages WHERE source=?',(sid,)).fetchone()[0]
    private = get(db,'private_sources',[])
    if any(m.get('type')=='user' and re.search(r'不要(?:记住|记录|保存|收集)|不(?:要)?存入.*(?:笔记|知识库)|do not (?:remember|store)',m.get('text',''),re.I) for m in messages):
        # Conservative opt-out: preserve the exclusion, never ingest this source.
        with db:
            put(db,'private_sources',list(set(private+[sid])))
            db.execute("UPDATE sources SET status='excluded',error='用户要求不记录，已排除本会话' WHERE id=?",(sid,))
            db.execute("UPDATE jobs SET status='excluded',error='用户要求不记录' WHERE source=? AND status IN ('pending','failed')",(sid,))
        return False
    with db:
        for message in messages:
            text = public_text(message)
            if not text.strip():
                continue
            # Large individual messages are split without truncation. Fragment ID retains original ID.
            pieces = [text[i:i+cfg['chunk_chars']] for i in range(0,len(text),cfg['chunk_chars'])]
            for index,piece in enumerate(pieces):
                mid = message['id'] if len(pieces)==1 else f"{message['id']}#part{index+1}/{len(pieces)}"
                existing = db.execute('SELECT 1 FROM messages WHERE source=? AND id=?',(sid,mid)).fetchone()
                if existing:
                    continue
                position += 1
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)',(sid,mid,position,
                    message['time']['created'],message['type'],digest(piece),piece))
        make_jobs(db,sid,cfg)
        db.execute('UPDATE sources SET captured_id=?,scanned_update=?,scanned_at=?,status=?,error=NULL WHERE id=?',
                   (messages[-1]['id'],source['updated'],now,'captured',sid))
    return True

def capture_batch(db,api,cfg,limit=3):
    private = set(get(db,'private_sources',[]))
    pilots = cfg.get('validation_sessions',[])
    rows = db.execute("""SELECT * FROM sources WHERE status!='excluded'
        AND scanned_at<? AND (scanned_update<updated OR scanned_at<?)
        ORDER BY CASE WHEN scanned_at>0 AND scanned_update<updated THEN 0 ELSE 1 END,
          CASE WHEN updated>? THEN 0 ELSE 1 END,scanned_at ASC,updated DESC""",
        (time.time()-60,time.time()-1800,(time.time()-30*86400)*1000)).fetchall()
    chosen = [r for r in rows if r['id'] not in private and
              (not cfg.get('validation_only') or r['id'] in pilots)][:limit]
    for source in chosen:
        try:
            capture(db,api,cfg,source)
        except Exception as error:
            with db:
                db.execute('UPDATE sources SET error=?,scanned_at=? WHERE id=?',(str(error)[:1000],time.time(),source['id']))
            log('capture_failed',source=source['id'],error=str(error))
