"""Bounded, non-exhaustive memory windows. Original evidence stays in messages."""
import json
import re
import time
from storage import digest, get, put

SIGNAL = re.compile(r'决定|确定|最终|记住|偏好|以后|长期|目标|打算|计划|需要|想要|不要|取消|改成|结论|原因|解决|成功|失败|阻塞|尚未|未完成|待办|next|prefer|decid', re.I)
ACK = re.compile(r'^(?:好的?|可以|继续|执行|嗯|收到|谢谢|OK|PONG|READY)[。！!,.\s]*$', re.I)

def schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS focus_ranges(
      job TEXT,source TEXT,first_position INTEGER,last_position INTEGER,
      selected TEXT,selected_chars INTEGER,original_chars INTEGER,
      status TEXT DEFAULT 'pending',PRIMARY KEY(job,source));
    CREATE INDEX IF NOT EXISTS focus_source ON focus_ranges(source,last_position);
    ''')

def prose(kind,text):
    if kind=='assistant':
        parts=re.findall(r'\[助手陈述，需核实\] (.*?)(?=\n\n\[(?:工具结果|助手陈述|执行错误)|\Z)',text,re.S)
        text='\n'.join(parts) if parts else ('' if '[工具结果 ' in text else text)
    text=re.sub(r'```.*?(?:```|\Z)','[代码/日志略过，可按原消息回查]',text,flags=re.S)
    lines=[line for line in text.splitlines() if len(line)<1500 and
           not re.match(r'^\s*(?:at |\d+: |<[/!a-zA-Z]|[{}\[\]]\s*$)',line)]
    return '\n'.join(lines).strip()

def excerpt(text,limit=1100):
    if len(text)<=limit:return text
    key=[line for line in text.splitlines() if SIGNAL.search(line)]
    if key:
        return ('\n'.join(key))[:limit]+'\n[仅重点摘录；其他原文未精读]'
    return text[:limit//2]+'\n[中间略过，可回查]\n'+text[-limit//2:]

def window(rows,limit):
    candidates=[]
    for row in rows:
        text=prose(row['kind'],row['text'])
        if not text or ACK.fullmatch(text):continue
        candidates.append((row,text))
    # Explicit mid-conversation corrections/decisions survive tail-only selection.
    first_user=next((r['id'] for r,t in candidates if r['kind']=='user'),None)
    recent={r['id'] for r,t in candidates[-8:]}
    anchors={r['id'] for r,t in candidates[-3:]}|{first_user}
    ranked=sorted(candidates,key=lambda x:(
        0 if x[0]['id'] in anchors else
        1 if x[0]['kind']=='user' and SIGNAL.search(x[1]) else
        2 if x[0]['id'] in recent else 3,
        -x[0]['position']))
    selected=[];size=0;seen=set()
    for row,text in ranked:
        if row['id']!=first_user and row['id'] not in recent and not SIGNAL.search(text):continue
        body=excerpt(text)
        key=digest(body)
        if key in seen:continue
        if size+len(body)+160>limit:continue
        seen.add(key);selected.append((row,body));size+=len(body)+160
    selected.sort(key=lambda item:item[0]['position'])
    return selected

def prepare(db,cfg,limit_sources=4):
    """Create durable windows and supersede old jobs only in the same transaction."""
    schema(db)
    if db.execute("SELECT 1 FROM jobs WHERE id LIKE 'focus_%' AND (status IN ('pending','processing') OR (status='failed' AND attempts<? AND next_at<?)) LIMIT 1",
                  (cfg['max_attempts'],time.time())).fetchone():return
    sources=db.execute('''SELECT s.*,COALESCE((SELECT MAX(last_position) FROM focus_ranges f
         WHERE f.source=s.id),0) focus_cursor FROM sources s
       WHERE s.status!='excluded' AND EXISTS(SELECT 1 FROM messages m WHERE m.source=s.id
         AND m.position>COALESCE((SELECT MAX(last_position) FROM focus_ranges f WHERE f.source=s.id),0))
       ORDER BY CASE WHEN EXISTS(SELECT 1 FROM focus_ranges f WHERE f.source=s.id) THEN 0 ELSE 1 END,
         s.archived,s.updated DESC''').fetchall()
    if cfg.get('validation_only'):
        sources=[s for s in sources if s['id'] in cfg.get('validation_sessions',[])]
    if not sources:return
    # New increments may run first; rotate historical directories to prevent one
    # long debugging conversation consuming every remaining daily submission.
    previous=get(db,'focus_last_directory','')
    def priority(s):
        last=db.execute('SELECT MAX(created) FROM messages WHERE source=? AND position>?',
                        (s['id'],s['focus_cursor'])).fetchone()[0] or 0
        live=s['focus_cursor']>0 and last>(time.time()-86400)*1000
        return (0 if live else 1, s['directory']==previous, s['archived'],-last)
    sources.sort(key=priority)
    group=sources[0]['directory'];packs=[];pack_chars=0
    limit_sources=cfg.get('focus_batch_sources',limit_sources)
    for source in sources:
        if source['directory']!=group and packs and not cfg.get('focus_mix_small_batches',True):continue
        rows=db.execute('SELECT * FROM messages WHERE source=? AND position>? ORDER BY position',
                        (source['id'],source['focus_cursor'])).fetchall()
        selected=window(rows,cfg.get('focus_window_chars',10000))
        selected_chars=sum(len(body)+160 for row,body in selected)
        if packs and pack_chars+selected_chars>cfg.get('focus_batch_chars',48000):continue
        packs.append((source,rows,selected))
        pack_chars+=selected_chars
        if len(packs)>=limit_sources:break
    create_pack(db,packs)
    with db:put(db,'focus_last_directory',group)

def create_pack(db,packs):
    key='focus_'+digest('|'.join(s['id']+':'+str(rows[-1]['position']) for s,rows,sel in packs))[:24]
    blocks=[];refs=[]
    for source,rows,selected in packs:
        blocks.append(f"\n来源会话 {source['id']}；标题 {source['title']}；目录 {source['directory']}\n")
        for row,body in selected:
            refs.append({'id':row['id'],'hash':row['hash'],'position':row['position'],'source':source['id']})
            blocks.append(f"[{row['kind']} {row['id']} 时间毫秒={int(row['created'])}]\n{body}")
    with db:
        primary,rows,_=packs[0]
        status='pending' if refs else 'focus_screened'
        db.execute('''INSERT OR IGNORE INTO jobs(id,source,first_id,last_id,position,evidence,refs,status,created,receipt)
          VALUES(?,?,?,?,?,?,?,?,?,?)''',(key,primary['id'],rows[0]['id'],rows[-1]['id'],rows[-1]['position'],
          '\n\n'.join(blocks),json.dumps(refs,ensure_ascii=False),status,time.time(),
          json.dumps({'reason':'本地重点窗口无可提交正文；未精读原文','checks':[]}) if not refs else None))
        for source,rows,selected in packs:
            db.execute('INSERT OR IGNORE INTO focus_ranges VALUES(?,?,?,?,?,?,?,?)',
              (key,source['id'],rows[0]['position'],rows[-1]['position'],json.dumps([r['id'] for r,b in selected]),
               sum(len(b) for r,b in selected),sum(len(r['text']) for r in rows),status))
            db.execute("""UPDATE jobs SET status='superseded',error=? WHERE source=?
              AND id NOT LIKE 'focus_%' AND status IN ('pending','failed') AND position<=?""",
              ('由重点筛选接管：'+key+'；未选原文保留可回查',source['id'],rows[-1]['position']))
    return key
