"""Authenticated encrypted snapshots on an independent, fast-forward Git branch.

Called by the root-owned backup application, never by a public HTTP endpoint.
Only ciphertext and nonsecret format metadata enter the repository.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time

TARGET = 'git@github-backup:daniei-chen/WorkBuddy-API.git'
BRANCH = 'workbuddy-recovery-encrypted'
CERTIFICATE = Path('/root/backups/workbuddy-recovery/recovery-public.pem')
CONFIG = Path('/root/backups/workbuddy-recovery/offsite.json')
REPOSITORY = Path('/root/backups/workbuddy-offsite.git')
MAX_CIPHER_BYTES = 50 * 1024 * 1024

def _run(command, *, data=None, cwd=None, accepted=(0,)):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', LC_ALL='C.UTF-8')
    process = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, cwd=cwd, env=env, timeout=180)
    if process.returncode not in accepted:
        # Credential helpers may include sensitive diagnostics. Never print them.
        raise RuntimeError(f'backup command {Path(command[0]).name} failed ({process.returncode})')
    return process

def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):digest.update(block)
    return digest.hexdigest()

def _oid(raw):
    value = raw.decode('ascii').strip()
    if not re.fullmatch(r'[0-9a-f]{40}', value):raise RuntimeError('invalid Git object identity')
    return value

def encrypt(snapshot: Path, certificate: Path, output: Path):
    if not snapshot.is_file() or snapshot.is_symlink():raise RuntimeError('invalid snapshot')
    if not certificate.is_file() or certificate.is_symlink():raise RuntimeError('invalid recovery certificate')
    _run(['openssl','cms','-encrypt','-binary','-in',str(snapshot),'-out',str(output),
          '-outform','DER','-aes-256-gcm','-recip',str(certificate),
          '-keyopt','rsa_padding_mode:oaep','-keyopt','rsa_oaep_md:sha256'])
    output.chmod(0o600)
    if output.stat().st_size>MAX_CIPHER_BYTES:raise RuntimeError('encrypted snapshot exceeds repository size limit')

def publish(snapshot: Path, certificate: Path, repository: Path, target=TARGET):
    """The optional target supports isolated tests; production uses the fixed target."""
    snapshot,certificate,repository=map(Path,(snapshot,certificate,repository))
    repository.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if repository.is_symlink():raise RuntimeError('backup repository must not be a symlink')
    if not repository.exists():_run(['git','init','--bare',str(repository)])
    repository.chmod(0o700)
    def git(*args, data=None):return _run(['git','--git-dir',str(repository),*args],data=data)
    bare=git('rev-parse','--is-bare-repository').stdout.strip()
    if bare!=b'true':raise RuntimeError('backup repository must be bare')
    remote=_run(['git','ls-remote','--exit-code',target,'refs/heads/'+BRANCH],accepted=(0,2))
    parent=None
    if remote.returncode==0:
        parent=_oid(remote.stdout.split()[0])
        git('fetch','--no-tags',target,'refs/heads/'+BRANCH+':refs/remotes/backup/encrypted')
        if _oid(git('rev-parse','refs/remotes/backup/encrypted').stdout)!=parent:
            raise RuntimeError('backup branch changed during fetch; retry later')
    with tempfile.TemporaryDirectory(prefix='encrypted-',dir=repository.parent) as temporary:
        cipher=Path(temporary)/'snapshot.cms'
        encrypt(snapshot,certificate,cipher)
        metadata={'format':'workbuddy-recovery-cms-v1','created_at':int(time.time()),
                  'cipher_sha256':_hash(cipher),'cipher_bytes':cipher.stat().st_size,
                  'certificate_sha256':_hash(certificate),'encryption':'AES-256-GCM / RSA-OAEP-SHA256'}
        blobs={
          'snapshot.cms':_oid(git('hash-object','-w',str(cipher)).stdout),
          'snapshot.json':_oid(git('hash-object','-w','--stdin',data=json.dumps(metadata,sort_keys=True).encode()).stdout),
          'README.md':_oid(git('hash-object','-w','--stdin',data=b'Encrypted WorkBuddy recovery snapshot. Recovery private key is held offline by the operator. No plaintext application data is stored on this branch.\n').stdout),
        }
        tree=_oid(git('mktree',data=''.join(f'100644 blob {digest}\t{name}\n' for name,digest in sorted(blobs.items())).encode()).stdout)
        command=['git','--git-dir',str(repository),'-c','user.name=WorkBuddy Backup',
                 '-c','user.email=backup@localhost','commit-tree',tree]
        if parent:command+=['-p',parent]
        commit=_oid(_run(command,data=b'Encrypted application recovery snapshot\n').stdout)
        # Normal push is intentional: races fail; no force and no main mutation.
        git('push',target,commit+':refs/heads/'+BRANCH)
        remote=_run(['git','ls-remote','--exit-code',target,'refs/heads/'+BRANCH])
        if _oid(remote.stdout.split()[0])!=commit:raise RuntimeError('remote backup verification failed')
        git('update-ref','refs/heads/'+BRANCH,commit)
    return dict(metadata,commit=commit,branch=BRANCH,verified_remote=True)

def configured_publish(snapshot: Path):
    configuration=json.loads(CONFIG.read_text())
    if configuration.get('target')!=TARGET or configuration.get('branch')!=BRANCH:
        raise RuntimeError('unexpected production backup target')
    if configuration.get('certificate_sha256')!=_hash(CERTIFICATE):
        raise RuntimeError('recovery certificate identity mismatch')
    return publish(snapshot,CERTIFICATE,REPOSITORY)
