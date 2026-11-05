"""Private durable ledger. Never opens or writes OpenCode's database."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state'
STATE.mkdir(exist_ok=True)

def config():
    return json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))

def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def redact(text):
    text = str(text)
    text = re.sub(r'(?i)\b(?:sk-|oc_client_|gh[pousr]_)[A-Za-z0-9_\-]{12,}', '[REDACTED]', text)
    text = re.sub(r'(?i)(authorization["\s:=]+(?:bearer\s+|basic\s+)?)[^\s"\',}]+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|密码|密钥)["\s]*[:=]["\s]*)([^\s,"\'}]{4,})', r'\1[REDACTED]', text)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED PRIVATE KEY]', text, flags=re.S)
    return text

def log(event, **values):
    row = {'time': dt.datetime.now(dt.timezone.utc).isoformat(), 'event': event, **values}
    with (STATE / 'worker.jsonl').open('a', encoding='utf-8') as out:
        out.write(redact(json.dumps(row, ensure_ascii=False)) + '\n')

def connect():
    db = sqlite3.connect(STATE / 'ledger.sqlite3', timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sources(
      id TEXT PRIMARY KEY, title TEXT, directory TEXT, archived INTEGER DEFAULT 0,
      updated REAL DEFAULT 0, scanned_update REAL DEFAULT -1, scanned_at REAL DEFAULT 0,
      captured_id TEXT, acknowledged_id TEXT, status TEXT DEFAULT 'pending', error TEXT,
      info TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(
      source TEXT, id TEXT, position INTEGER, created REAL, kind TEXT, hash TEXT, text TEXT,
      PRIMARY KEY(source,id));
    CREATE TABLE IF NOT EXISTS jobs(
      id TEXT PRIMARY KEY, source TEXT, first_id TEXT, last_id TEXT, position INTEGER,
      evidence TEXT, refs TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
      next_at REAL DEFAULT 0, created REAL, started REAL, finished REAL,
      writer TEXT, prompt_id TEXT, model TEXT, receipt TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS verified_messages(id TEXT, hash TEXT, job TEXT,
      PRIMARY KEY(id,hash));
    CREATE TABLE IF NOT EXISTS models(key TEXT PRIMARY KEY, cooldown REAL DEFAULT 0,
      successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0, error TEXT);
    CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,next_at,created);
    CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source,position);
    ''')
    return db

def get(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default

def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

@contextlib.contextmanager
def exclusive_lock():
    handle = (STATE / 'worker.lock').open('a+b')
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if handle.read(1) == b'':
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        handle.close()

def atomic_json(file, value):
    file = Path(file)
    temp = file.with_suffix(file.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, file)
