"""Run on the existing Windows WSL node as its operator. Additive, backed up install.

Stop/restart is deliberately left to the service manager, after this script's
idle check and consistent SQLite backup. No credentials are printed.
"""
import argparse
import fcntl
import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def install(root, incoming):
    root=Path(root).resolve();incoming=Path(incoming).resolve()
    # The gateway holds this lock for its lifetime. Refuse edits until its
    # service manager has completed a graceful stop, eliminating acceptance
    # races between an idle preflight and replacing source files.
    lock=(root/'data/gateway.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Stop the idle gateway gracefully with its service manager before deploying') from None
    try:return install_stopped(root,incoming)
    finally:lock.close()


def install_stopped(root, incoming):
    dbpath=root/'data/jobs.sqlite3'
    with sqlite3.connect(dbpath) as source:
        busy=source.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','loading','running','saving','unloading')").fetchone()[0]
        if busy:raise RuntimeError('Node has active tasks; deployment deferred')
        backup=root/'backups'/('classroom-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(parents=True,mode=0o700)
        with sqlite3.connect(backup/'jobs.sqlite3') as target:
            source.backup(target)
            if target.execute('pragma integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup integrity check failed')
    for relative in ['server','workers','config.toml']:
        path=root/relative
        if path.is_dir():shutil.copytree(path,backup/relative,ignore=shutil.ignore_patterns('__pycache__'))
        else:shutil.copy2(path,backup/relative)
    app=root/'server/app.py';text=app.read_text()
    marker='from .classroom import install as install_classroom'
    if marker not in text:
        anchor='app = FastAPI('
        line=next(line for line in text.splitlines() if line.startswith(anchor))
        text=text.replace(line,line+'\n'+marker+'\ninstall_classroom(app)',1)
        anchor='        while not self.stopping:\n'
        replacement=anchor+"            classroom = getattr(self, 'classroom', None)\n            if classroom and (classroom.reserved() or classroom.guard.locked()):\n                await asyncio.sleep(.25)\n                continue\n"
        # Only the dispatch loop, before execute(), is eligible.
        start=text.index('    async def schedule(self):');end=text.index('    async def execute(',start)
        text=text[:start]+text[start:end].replace(anchor,replacement,1)+text[end:]
        text=text.replace('    async def stop(self):\n        self.stopping = True',
            "    async def stop(self):\n        self.stopping = True\n        classroom = getattr(self, 'classroom', None)\n        if classroom:\n            if classroom.timer: classroom.timer.cancel()\n            await classroom.close()",1)
    # The existing official worker accepts the same bounded token ceiling as
    # the Mac client. Retain all job options and identities during this upgrade.
    text=text.replace('not 128 <= tokens <= 2048:', 'not 128 <= tokens <= 8192:')
    compile(text,str(app),'exec')
    prepared={}
    for relative in ['server/classroom.py','workers/classroom_worker.py']:
        data=(incoming/relative).read_bytes();compile(data,relative,'exec');prepared[relative]=data
    preparation=incoming/'scripts/prepare_knowledge.py'
    if preparation.is_file():
        data=preparation.read_bytes();compile(data,'scripts/prepare_knowledge.py','exec')
        prepared['scripts/prepare_knowledge.py']=data
    for relative,data in prepared.items():
        (root/relative).parent.mkdir(parents=True,exist_ok=True)
        temporary=(root/relative).with_suffix('.py.deploy-part')
        temporary.write_bytes(data);temporary.replace(root/relative)
    app.write_text(text)
    manifest={'backup':str(backup),'files':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in ['server/app.py','server/classroom.py','workers/classroom_worker.py']}}
    (backup/'deployment.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--incoming',required=True);a=p.parse_args();install(a.root,a.incoming)
