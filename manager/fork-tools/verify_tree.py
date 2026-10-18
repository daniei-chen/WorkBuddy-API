#!/usr/bin/env python3
"""离线复现 source_preflight 的校验逻辑（可指定任意 root/信任锚），用于发布前自检。"""
import hashlib, json, stat, subprocess, sys
from pathlib import Path, PurePosixPath

SAFE_MODES={0o400,0o440,0o444,0o600,0o640,0o644,0o700,0o750,0o755}
ENTRYPOINTS={'deploy/source_preflight.py','server/backup_application.py'}

def check(root: Path, trust: Path) -> int:
    marker = root/'deploy/managed-release.json'
    sig = root/'deploy/managed-release.sig'
    if not marker.is_file() or not sig.is_file():
        print('missing manifest/sig'); return 1
    r = subprocess.run(['openssl','dgst','-sha256','-verify',str(trust),
                        '-signature',str(sig),str(marker)], capture_output=True, text=True)
    print('signature:', (r.stdout or r.stderr).strip())
    if r.returncode: return 1
    release = json.loads(marker.read_text())
    if 'deploy/managed-release.json' in release['files']:
        print('manifest hashes itself'); return 1
    bad = 0
    for rel, item in release['files'].items():
        p = PurePosixPath(rel)
        f = root/rel
        if p.is_absolute() or '..' in p.parts or f.is_symlink() or not f.is_file():
            print('unsafe/missing:', rel); bad += 1; continue
        mode = stat.S_IMODE(f.stat().st_mode)
        if mode != item['mode'] or mode not in SAFE_MODES:
            print('mode mismatch:', rel, oct(mode), oct(item['mode'])); bad += 1
        if rel in ENTRYPOINTS and not mode & 0o111:
            print('entrypoint not exec:', rel); bad += 1
        if hashlib.sha256(f.read_bytes()).hexdigest() != item['candidate_sha256']:
            print('hash mismatch:', rel); bad += 1
    # 反向：发布树里有没有清单外的文件（preflight 不查，但发布应完整）
    listed=set(release['files'])
    print(f'files checked: {len(listed)}, problems: {bad}')
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(check(Path(sys.argv[1]), Path(sys.argv[2])))
