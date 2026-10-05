#!/opt/workbuddy-manager/venv/bin/python
"""Read-only production source and permission gate. No startup patching."""
from pathlib import Path, PurePosixPath
import hashlib,json,stat,subprocess

SAFE_MODES={0o400,0o440,0o444,0o600,0o640,0o644,0o700,0o750,0o755}
ENTRYPOINTS={'deploy/source_preflight.py','server/backup_application.py'}

def check(root):
    marker=root/'deploy/managed-release.json'
    verified=0
    if marker.exists():
        release=json.loads(marker.read_text())
        if release.get('signature_required'):
            trust=Path('/opt/workbuddy-manager/release-trust.pem')
            signature=root/'deploy/managed-release.sig'
            if not trust.is_file() or trust.is_symlink() or not signature.is_file():
                raise RuntimeError('release signature or installed trust anchor missing')
            checked=subprocess.run(['openssl','dgst','-sha256','-verify',str(trust),
                                    '-signature',str(signature),str(marker)],
                                   capture_output=True,timeout=10)
            if checked.returncode:
                raise RuntimeError('managed release signature verification failed')
        if 'deploy/managed-release.json' in release['files']:
            raise RuntimeError('release marker must not hash itself')
        for relative,item in release['files'].items():
            parts=PurePosixPath(relative)
            if parts.is_absolute() or '..' in parts.parts or '\\' in relative:
                raise RuntimeError('unsafe managed release path')
            file=root/relative
            if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(root.resolve()):
                raise RuntimeError('unsafe managed release file: '+relative)
            expected=item.get('mode')
            actual=stat.S_IMODE(file.stat().st_mode)
            if isinstance(expected,bool) or expected not in SAFE_MODES or expected!=actual:
                raise RuntimeError('managed release permissions mismatch: '+relative)
            if relative in ENTRYPOINTS and not actual&0o111:
                raise RuntimeError('managed entrypoint is not executable: '+relative)
            if hashlib.sha256(file.read_bytes()).hexdigest()!=item['candidate_sha256']:
                raise RuntimeError('managed source hash mismatch: '+relative)
            verified+=1
    for file in (root/'server').rglob('*.py'):
        if '__pycache__' not in file.parts:
            compile(file.read_text(encoding='utf-8'),str(file),'exec')
    return verified

if __name__=='__main__':
    verified=check(Path('/opt/workbuddy-manager'))
    print(f'managed source preflight passed: {verified} files; startup patches disabled')

