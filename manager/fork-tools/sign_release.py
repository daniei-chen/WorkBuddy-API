#!/usr/bin/env python3
"""为 WorkBuddy Manager 发布树生成并签名 deploy/managed-release.json。

格式与现网 source_preflight.py 校验的一致：
  * JSON: {"signature_required": true, "files": {"<rel>": {"candidate_sha256": "...", "mode": 420}}}
  * 签名: openssl dgst -sha256 -sign <private.key> deploy/managed-release.json
  * 验签: /opt/workbuddy-manager/release-trust.pem 里的公钥

托管范围与现网清单一致：server/、web/out/、deploy/、local-patches/。
排除：__pycache__、*.pyc、deploy/managed-release.json 自身、deploy/managed-release.sig。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

INCLUDE_ROOTS = ('server', 'web/out', 'deploy', 'local-patches')
EXCLUDE_NAMES = {'__pycache__', '.git'}
EXCLUDE_FILES = {'deploy/managed-release.json', 'deploy/managed-release.sig'}


def build_manifest(root: Path, version: str) -> dict:
    files: dict[str, dict] = {}
    for inc in INCLUDE_ROOTS:
        base = root / inc
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_NAMES)
            for name in sorted(filenames):
                p = Path(dirpath) / name
                rel = p.relative_to(root).as_posix()
                if rel in EXCLUDE_FILES or rel.endswith('.pyc'):
                    continue
                if p.is_symlink() or not p.is_file():
                    continue
                mode = stat.S_IMODE(p.stat().st_mode)
                files[rel] = {
                    'candidate_sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                    'mode': mode,
                }
    return {
        'version': version,
        'at': int(time.time()),
        'database_rollback': False,
        'signature_required': True,
        'files': files,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True, help='发布树根目录（含 server/ web/ deploy/）')
    ap.add_argument('--key', required=True, help='RSA 私钥（openssl genpkey 生成）')
    ap.add_argument('--pub', help='公钥文件；给出时顺带验签自检')
    ap.add_argument('--version', default='')
    args = ap.parse_args()

    root = Path(args.root).resolve()
    version = args.version or (root / '.version').read_text(encoding='utf-8').strip()
    manifest = build_manifest(root, version)
    out = root / 'deploy' / 'managed-release.json'
    sig = root / 'deploy' / 'managed-release.sig'
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
    subprocess.run(['openssl', 'dgst', '-sha256', '-sign', args.key,
                    '-out', str(sig), str(out)], check=True)
    print(f'manifest: {len(manifest["files"])} files -> {out}')
    print(f'signature -> {sig}')
    if args.pub:
        r = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', args.pub,
                            '-signature', str(sig), str(out)],
                           capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
        if r.returncode:
            return 1
        print('self-verify OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
