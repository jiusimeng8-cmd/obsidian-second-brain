"""Receipts are claims until checked against tool traces and vault files."""
import json
from pathlib import Path
import re
from storage import digest

def text_of(message):
    return '\n'.join(p.get('text','') for p in message.get('content',[]) if p.get('type')=='text')

def tool_calls(messages):
    result=[]
    for message in messages:
        for part in message.get('content',[]):
            if part.get('type') != 'tool':
                continue
            state=part.get('state',{})
            if state.get('status') != 'completed':
                continue
            calls=state.get('metadata',{}).get('toolCalls',[])
            for call in calls:
                if call.get('status')=='completed':
                    result.append(call)
            if part.get('name','').startswith('obsidian'):
                result.append({'tool':part['name'],'input':state.get('input',{}),'status':'completed'})
    return result

def safe_note(vault, relative):
    if not isinstance(relative,str) or not relative.endswith('.md'):
        raise ValueError('回执必须列出 Markdown 相对路径')
    root=Path(vault).resolve()
    target=(root/relative).resolve()
    if not target.is_relative_to(root) or any(x in relative.replace('\\','/').split('/') for x in ('.obsidian','.trash','凭据')):
        raise ValueError('回执路径越界或指向凭据')
    return target

def snapshot(vault):
    root=Path(vault)
    result={}
    for file in root.rglob('*.md'):
        rel=file.relative_to(root)
        if any(p.startswith('.') or p in ('凭据','attachments','logs') for p in rel.parts):
            continue
        result[rel.as_posix()]=digest(file.read_text(encoding='utf-8-sig'))
    return result

def read_covers(call,path,content,quote):
    args=call.get('input',{})
    if args.get('path')!=path:
        return False
    name=call['tool'].rsplit('.',1)[-1]
    if name in ('read_note','read-note'):
        return True
    if name=='read_note_lines':
        start=args.get('startLine');end=args.get('endLine')
        if isinstance(start,int) and isinstance(end,int) and 1<=start<=end:
            return quote in '\n'.join(content.splitlines()[start-1:end])
    return False

def receipt_from(messages,job_id):
    for message in reversed(messages):
        if message.get('type')!='assistant':
            continue
        text=text_of(message)
        for match in re.finditer(r'BRAIN_RECEIPT\s*(\{[^\n]+\})',text):
            try:
                receipt=json.loads(match.group(1))
                if receipt.get('job')==job_id:
                    return receipt
            except json.JSONDecodeError:
                pass
    return None

def verify(job,receipt,messages,cfg,before):
    if not receipt or receipt.get('job')!=job['id']:
        raise ValueError('缺少本任务专属 JSON 回执')
    calls=tool_calls(messages)
    status=receipt.get('status')
    checks=[]
    if set(receipt.get('reviewed_refs',[])) != {r['id'] for r in json.loads(job['refs'])}:
        raise ValueError('回执未覆盖完整消息范围')
    if status=='no_value':
        if job['id'].startswith('focus_'):
            if not isinstance(receipt.get('reason'),str) or len(receipt['reason'].strip())<8:
                raise ValueError('重点筛选未提取必须说明具体原因')
            return {'status':'focus_screened','reason':receipt['reason'],'checks':[]}
        reason=receipt.get('reason','')
        if not reason or not receipt.get('reviewed_refs') or set(receipt['reviewed_refs']) != {r['id'] for r in json.loads(job['refs'])}:
            raise ValueError('无价值跳过必须解释并覆盖完整消息范围')
        # Highly conservative: only acknowledgments/test output can skip without a note.
        for line in job['evidence'].splitlines():
            if re.match(r'^\[(?:user|assistant) msg_.*时间毫秒=\d+\]$',line) or not line.strip():
                continue
            if not re.fullmatch(r'(?:好(?:的)?|可以|嗯|继续|谢谢|收到|OK|PONG|READY)[。！!,.\s]*',line,re.I):
                raise ValueError('含实质内容的 no_value 需人工复核，不能自动确认')
        return {'status':'skipped_verified','reason':reason,'checks':[]}
    if status not in ('written','duplicate'):
        raise ValueError('失败或未知回执，不提交游标')
    notes=receipt.get('notes',[])
    if not notes:
        raise ValueError('写入或重复必须指向真实笔记')
    refs=json.loads(job['refs'])
    # Index provenance can follow a link to a primary note read in this receipt.
    cited_primaries=[]
    for item in notes:
        name=item.get('path')
        body=safe_note(cfg['vault'],name).read_text(encoding='utf-8-sig')
        if any(r.get('source',job['source']) in body and r['id'].split('#')[0] in body for r in refs):
            if any(read_covers(c,name,body,item.get('quote','')) for c in calls):
                cited_primaries.append(name)
    for note in notes:
        relative=note.get('path')
        file=safe_note(cfg['vault'],relative)
        content=file.read_text(encoding='utf-8-sig')
        quote=note.get('quote','').strip()
        if len(quote)<12 or quote not in content:
            raise ValueError('回读未找到足够明确的逐字证据: '+str(relative))
        reads=[c for c in calls if read_covers(c,relative,content,quote)]
        if not reads:
            raise ValueError('缺少实际 MCP 回读: '+relative)
        current=digest(content)
        if status=='written':
            direct=any(r.get('source',job['source']) in content and r['id'].split('#')[0] in content for r in refs)
            index=Path(relative).stem.endswith(('索引','总览')) or relative.startswith('00-入口/')
            links=re.findall(r'\[\[([^\]|#]+)(?:[^\]]*)\]\]',content)
            indirect=index and any(p.removesuffix('.md') in links or Path(p).stem in links for p in cited_primaries if p!=relative)
            if not direct and not indirect:
                raise ValueError('笔记缺少本来源会话和消息范围: '+relative)
            if current==before.get(relative):
                raise ValueError('written 无实际变化，请核对既有记录后报告 duplicate')
            writes=[c for c in calls if any(x in c['tool'] for x in ('write_note','patch_note','update_frontmatter'))
                    and c.get('input',{}).get('path')==relative]
            if not writes:
                raise ValueError('缺少对应笔记实际写入调用: '+relative)
        checks.append({'path':relative,'quote':quote,'hash':current})
    return {'status':'written_verified' if status=='written' else 'skipped_verified',
            'reason':receipt.get('reason',''),'checks':checks}
