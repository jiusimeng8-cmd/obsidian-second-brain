"""Authenticated loopback API adapter for OpenCode / OpenChamber."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from storage import redact

def settings_path():
    """Where the desktop client stores its local port and client token."""
    override = os.environ.get('SECOND_BRAIN_SETTINGS')
    return Path(override) if override else Path.home() / '.config/openchamber/settings.json'

class APIError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(redact(f'HTTP {status}: {message}')[:1200])

class API:
    def __init__(self):
        self.settings = settings_path()

    def call(self, route, body=None, method=None, query=None, timeout=25):
        settings = json.loads(self.settings.read_text(encoding='utf-8'))
        port = int(settings['desktopLocalPort'])
        url = f'http://127.0.0.1:{port}' + route
        if query:
            url += '?' + urllib.parse.urlencode({k:v for k,v in query.items() if v is not None})
        headers = {'Authorization': 'Bearer ' + settings['desktopLocalClientToken']}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise APIError(error.code, error.read(4096).decode('utf-8', errors='replace')) from None

    def session(self, sid):
        return self.call(f'/api/session/{sid}')['data']

    def sessions(self, archived=False):
        query = {'limit':100, 'order':'desc', 'archived':str(archived).lower()}
        while True:
            page = self.call('/api/session', query=query)
            yield from page['data']
            cursor = page.get('cursor', {}).get('next')
            if not cursor:
                break
            query = {'limit':100, 'cursor':cursor}

    def messages(self, sid, after=None):
        query = {'limit':100, 'order':'desc'}
        result = []
        found = after is None
        while True:
            page = self.call(f'/api/session/{sid}/message', query=query)
            stop = False
            for message in page['data']:
                if message['id'] == after:
                    found = True; stop = True; break
                result.append(message)
            cursor = page.get('cursor', {}).get('next')
            if stop or not cursor:
                break
            query = {'limit':100, 'cursor':cursor}
        result.reverse()
        return result, found

    def update(self, sid, **fields):
        return self.call(f'/api/session/{sid}', fields, method='PATCH')

    def focus_messages(self, sid, after=None):
        """Bounded first-pass sampling; skipped pages are explicitly not reviewed."""
        newest=self.call(f'/api/session/{sid}/message',query={'limit':100,'order':'desc'})
        rows=list(newest['data']);cursor=newest.get('cursor',{}).get('next')
        if cursor and not any(m['id']==after for m in rows):
            page=self.call(f'/api/session/{sid}/message',query={'limit':100,'cursor':cursor})
            rows+=page['data'];cursor=page.get('cursor',{}).get('next')
        found=after is None or any(m['id']==after for m in rows)
        if after and found:
            rows=rows[:next(i for i,m in enumerate(rows) if m['id']==after)]
            cursor=None
        elif not after and cursor:
            opening=self.call(f'/api/session/{sid}/message',query={'limit':30,'order':'asc'})
            rows+=opening['data']
        unique={m['id']:m for m in rows}
        return sorted(unique.values(),key=lambda m:(m['time']['created'],m['id'])),found,bool(cursor)

    def prompt(self, sid, text, message_id, metadata):
        return self.call(f'/api/session/{sid}/prompt', {
            'id': message_id, 'text': text, 'metadata':metadata, 'delivery':'queue'
        })['data']

def public_text(message):
    """Preserve user text, final prose and tool evidence; exclude private reasoning."""
    kind = message.get('type')
    if kind == 'user':
        text = message.get('text', '')
        files = message.get('files', [])
        if files:
            text += '\n[附件元数据，未解析附件正文] ' + json.dumps(
                [{k:f.get(k) for k in ('filename','mime') if f.get(k)} for f in files], ensure_ascii=False)
        return redact(text)
    if kind != 'assistant':
        return ''
    blocks = []
    for part in message.get('content', []):
        if part.get('type') == 'text':
            blocks.append('[助手陈述，需核实] ' + part.get('text',''))
        elif part.get('type') == 'tool':
            state = part.get('state', {})
            output = '\n'.join(c.get('text','') for c in state.get('content',[]) if c.get('type') == 'text')
            calls = state.get('metadata',{}).get('toolCalls',[])
            names = [c.get('tool','') for c in calls]
            blocks.append('[工具结果 ' + part.get('name','') + ' / ' + ','.join(names) +
                          ' / ' + state.get('status','unknown') + ']\n' + output)
    if message.get('error'):
        blocks.append('[执行错误] ' + json.dumps(message['error'], ensure_ascii=False))
    return redact('\n\n'.join(blocks))
