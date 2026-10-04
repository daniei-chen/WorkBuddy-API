#!/usr/bin/env python3
"""本地补丁：缓存命中 token 采集（供「当天命中率」显示）。幂等，双版本兼容。

背景：上游 usage 里有 prompt_cache_hit_tokens / prompt_cache_miss_tokens，
但 manager 网关此前只取 credit/tokens，命中数据未落库 → 无法展示命中率。
方案：_record 增加 cache 参数，把 hit/miss 写入 request_logs 与 usage_daily
的新列；额度看板（quota-dashboard）与控制台用量统计读取后显示命中率。

兼容两个代码世代（按特征自动选择规则集）：
  * 1.0.37：_record 内联 SQL（request_logs 的 INSERT 在 gateway.py）
  * 1.0.57+：request_logs 写入下沉到 db.add_request_log(**kwargs)（2026-09 重构）
维护：由同目录 apply_patches.sh 在服务启动时自动重施。
回退：cp backup/gateway.py.orig-* server/routers/gateway.py
      并 cp backup/db.py.orig-* server/db.py 后重启。
"""
import os
import shutil
import sys

GW = '/opt/workbuddy-manager/server/routers/gateway.py'
DB = '/opt/workbuddy-manager/server/db.py'

# ── gateway.py 公共片段（两代相同）─────────────────────────

GW_HELPER = '''def _usage_cache_tokens(usage: dict | None) -> tuple[int, int] | None:
    """从 usage 里取缓存命中/未命中 token（prompt_cache_hit_tokens / miss）。

    上游在末帧 usage 里给出这两个字段（逆向实测与本地抓包证实）。取不到
    返回 None（存 NULL）——「没有数据」与「命中 0」在命中率里是两回事：
    前者不参与统计，后者是真实的 0% 命中。

    本地补丁（2026-09-16）：为「今日缓存命中率」显示采集数据。
    """
    if not isinstance(usage, dict):
        return None
    hit = usage.get('prompt_cache_hit_tokens')
    miss = usage.get('prompt_cache_miss_tokens')
    if hit is None and miss is None:
        return None
    try:
        h = int(hit) if hit is not None and not isinstance(hit, bool) else 0
        m = int(miss) if miss is not None and not isinstance(miss, bool) else 0
    except (TypeError, ValueError):
        return None
    return (max(h, 0), max(m, 0))


'''

GW_RECORD_OLD_SIG = (
    "def _record(key: dict | None, ip: str, model: str, mapped: str, status: int, "
    "pt: int, ct: int, latency: int, ua: str | None, error: str | None, stream: bool, "
    "*, credit: float | None = None, first_token: int | None = None) -> None:"
)
GW_RECORD_NEW_SIG = (
    "def _record(key: dict | None, ip: str, model: str, mapped: str, status: int, "
    "pt: int, ct: int, latency: int, ua: str | None, error: str | None, stream: bool, "
    "*, credit: float | None = None, first_token: int | None = None, "
    "cache: tuple[int, int] | None = None) -> None:"
)

GW_CALL_NONSTREAM_OLD = """            _record(
                key, ip, requested_model or '', mapped or '', resp.status_code, pt, ct,
                latency, ua, error, False, credit=_usage_credit(usage),
            )"""
GW_CALL_NONSTREAM_NEW = """            _record(
                key, ip, requested_model or '', mapped or '', resp.status_code, pt, ct,
                latency, ua, error, False, credit=_usage_credit(usage),
                cache=_usage_cache_tokens(usage),
            )"""

GW_CALL_STREAM_OLD = """            _record(
                key, ip, requested_model or '', mapped or '', status_code, pt, ct,
                latency, ua, error_text, True, credit=_usage_credit(usage),
                first_token=first_token_ms,
            )"""
GW_CALL_STREAM_NEW = """            _record(
                key, ip, requested_model or '', mapped or '', status_code, pt, ct,
                latency, ua, error_text, True, credit=_usage_credit(usage),
                first_token=first_token_ms, cache=_usage_cache_tokens(usage),
            )"""

# ── gateway.py 1.0.37 特有片段 ────────────────────────────

GW_INSERT_OLD = """            'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, stream, credit, realm) '
            'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (int(time.time()), key['id'] if key else None, ip, model, mapped, status, pt, ct, latency, first_token, ua, error, 1 if stream else 0, credit, realm),
"""
GW_INSERT_NEW = """            'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, stream, credit, realm, cache_hit_tokens, cache_miss_tokens) '
            'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (int(time.time()), key['id'] if key else None, ip, model, mapped, status, pt, ct, latency, first_token, ua, error, 1 if stream else 0, credit, realm, cache[0] if cache else None, cache[1] if cache else None),
"""

GW_BUMP_OLD = """            if total or credit:
                db.bump_usage(key['id'], model, pt, ct, credit, realm=realm)"""
GW_BUMP_NEW = """            if total or credit or cache:
                db.bump_usage(key['id'], model, pt, ct, credit, realm=realm,
                              cache_hit=cache[0] if cache else 0,
                              cache_miss=cache[1] if cache else 0)"""

# ── gateway.py 1.0.57+ 特有片段（add_request_log 重构后）────

GW_LOG157_OLD = """            credit=credit,
            realm=realm,
        )"""
GW_LOG157_NEW = """            credit=credit,
            realm=realm,
            cache_hit_tokens=cache[0] if cache else None,
            cache_miss_tokens=cache[1] if cache else None,
        )"""

GW_BUMP157_OLD = """            if total or credit:
                db.bump_usage(key['id'], model_clean, pt, ct, credit, realm=realm)"""
GW_BUMP157_NEW = """            if total or credit or cache:
                db.bump_usage(key['id'], model_clean, pt, ct, credit, realm=realm,
                              cache_hit=cache[0] if cache else 0,
                              cache_miss=cache[1] if cache else 0)"""

# ── db.py 公共片段 ───────────────────────────────────────

DB_SCHEMA_RL_OLD = """  -- NULL = 该字段上线前的历史记录（或模型名无前缀）——按 cn 归类，见 realm_of_model。
  realm             TEXT
);"""
DB_SCHEMA_RL_NEW = """  -- NULL = 该字段上线前的历史记录（或模型名无前缀）——按 cn 归类，见 realm_of_model。
  realm             TEXT,
  -- 缓存命中/未命中 token（本地补丁：上游 usage.prompt_cache_hit_tokens /
  -- prompt_cache_miss_tokens）。NULL = 上游未返回（历史记录），不参与命中率。
  cache_hit_tokens  INTEGER,
  cache_miss_tokens INTEGER
);"""

DB_SCHEMA_UD_OLD = """  -- 版本（cn / global）；NULL 归 cn。主键含它，使两个版本的同名模型分开累计。
  realm             TEXT    NOT NULL DEFAULT 'cn',
  PRIMARY KEY (day, key_id, model, realm)
);"""
DB_SCHEMA_UD_NEW = """  -- 版本（cn / global）；NULL 归 cn。主键含它，使两个版本的同名模型分开累计。
  realm             TEXT    NOT NULL DEFAULT 'cn',
  -- 缓存命中/未命中 token 当日累计（本地补丁，供命中率显示）。
  cache_hit_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, key_id, model, realm)
);"""

DB_MIG_ADD = """    # 缓存命中 token（本地补丁：今日命中率显示）。request_logs 用可空
    # （历史记录无数据，不参与统计）；usage_daily 用 0 默认值（累计列）。
    ('request_logs', 'cache_hit_tokens', 'INTEGER'),
    ('request_logs', 'cache_miss_tokens', 'INTEGER'),
    ('usage_daily', 'cache_hit_tokens', 'INTEGER NOT NULL DEFAULT 0'),
    ('usage_daily', 'cache_miss_tokens', 'INTEGER NOT NULL DEFAULT 0'),
)"""

# 1.0.37 的迁移表尾部锚点
DB_MIG_137_OLD = """    ('api_keys', 'realm', "TEXT NOT NULL DEFAULT ''"),
)"""
DB_MIG_137_NEW = ("""    ('api_keys', 'realm', "TEXT NOT NULL DEFAULT ''"),\n""" + DB_MIG_ADD)

# 1.0.57 的迁移表尾部锚点（此后还有 quota/reason 等条目，故用最后一条作锚点）
DB_MIG_157_OLD = """    ('ip_access_logs', 'reason', 'TEXT'),
)"""
DB_MIG_157_NEW = ("""    ('ip_access_logs', 'reason', 'TEXT'),\n""" + DB_MIG_ADD)

DB_BUMP_SIG_OLD = """def bump_usage(
    key_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    credit: float | None = None,
    realm: str | None = None,
) -> None:"""
DB_BUMP_SIG_NEW = """def bump_usage(
    key_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    credit: float | None = None,
    realm: str | None = None,
    cache_hit: int = 0,
    cache_miss: int = 0,
) -> None:"""

DB_BUMP_SQL_OLD = """    execute(
        'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit, realm) '
        'VALUES(?, ?, ?, 1, ?, ?, ?, ?) '
        'ON CONFLICT(day, key_id, model, realm) DO UPDATE SET '
        '  requests = requests + 1, '
        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '
        '  completion_tokens = completion_tokens + excluded.completion_tokens, '
        '  credit = credit + excluded.credit',
        (day, key_id, model, prompt_tokens, completion_tokens, float(credit or 0), r),
    )"""
DB_BUMP_SQL_NEW = """    execute(
        'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit, realm, cache_hit_tokens, cache_miss_tokens) '
        'VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(day, key_id, model, realm) DO UPDATE SET '
        '  requests = requests + 1, '
        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '
        '  completion_tokens = completion_tokens + excluded.completion_tokens, '
        '  credit = credit + excluded.credit, '
        '  cache_hit_tokens = cache_hit_tokens + excluded.cache_hit_tokens, '
        '  cache_miss_tokens = cache_miss_tokens + excluded.cache_miss_tokens',
        (day, key_id, model, prompt_tokens, completion_tokens, float(credit or 0), r,
         int(cache_hit or 0), int(cache_miss or 0)),
    )"""


def _apply(text: str, rules) -> tuple[str, str | None]:
    """逐条应用；返回 (新文本, 错误说明)。任一条计数异常即整体放弃。"""
    for old, new, label in rules:
        if text.count(old) != 1:
            return text, f'{label} 锚点不唯一/缺失（可能是上游改了代码）'
        text = text.replace(old, new)
    return text, None


def _save(path: str, text: str) -> None:
    bak = path + '.pre-patch'
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    open(path, 'w', encoding='utf-8').write(text)


def patch_gateway(path: str) -> int:
    if not os.path.isfile(path):
        print(f'[skip] {path} 不存在')
        return 0
    text = open(path, encoding='utf-8').read()
    if '_usage_cache_tokens' in text:
        print('[skip] gateway.py 已含缓存采集补丁')
        return 0
    # 2026-10-04：上游 1.0.79 起已原生采集缓存 token（gateway.py 里形如
    # `cache_hit_tokens=cache_hit,`），本补丁对这类代码世代已过时。
    # 明确 skip 而不是 [FAIL] —— 避免与「真正的锚点漂移」混在一起、掩盖真问题。
    if 'cache_hit_tokens=' in text:
        print('[skip] gateway.py 上游已原生采集缓存 token，本补丁无需施加')
        return 0
    anchor = 'def _usage_credit(usage: dict | None) -> float | None:'
    if text.count(anchor) != 1:
        print('[FAIL] gateway.py: _usage_credit 锚点不唯一，未打补丁')
        return 1
    text = text.replace(anchor, GW_HELPER + anchor)

    if 'db.add_request_log(' in text:
        specific = [
            (GW_LOG157_OLD, GW_LOG157_NEW, 'add_request_log 调用'),
            (GW_BUMP157_OLD, GW_BUMP157_NEW, 'bump_usage 调用(157)'),
        ]
    else:
        specific = [
            (GW_INSERT_OLD, GW_INSERT_NEW, 'INSERT 语句'),
            (GW_BUMP_OLD, GW_BUMP_NEW, 'bump_usage 调用'),
        ]
    common = [
        (GW_RECORD_OLD_SIG, GW_RECORD_NEW_SIG, '_record 签名'),
        (GW_CALL_NONSTREAM_OLD, GW_CALL_NONSTREAM_NEW, '非流式调用点'),
        (GW_CALL_STREAM_OLD, GW_CALL_STREAM_NEW, '流式调用点'),
    ]
    text, err = _apply(text, specific + common)
    if err:
        print(f'[FAIL] gateway.py: {err}，未落盘')
        return 1
    try:
        compile(text, path, 'exec')
    except SyntaxError as exc:
        print(f'[FAIL] gateway.py 补丁后语法错误，未落盘：{exc}')
        return 1
    _save(path, text)
    print('[OK] gateway.py 缓存采集补丁已施加')
    return 0


def patch_db(path: str) -> int:
    if not os.path.isfile(path):
        print(f'[skip] {path} 不存在')
        return 0
    text = open(path, encoding='utf-8').read()
    if 'cache_hit_tokens' in text:
        print('[skip] db.py 已含缓存列补丁')
        return 0

    if 'def add_request_log(' in text:
        mig_rule = (DB_MIG_157_OLD, DB_MIG_157_NEW, '_MIGRATIONS 迁移表')
    else:
        mig_rule = (DB_MIG_137_OLD, DB_MIG_137_NEW, '_MIGRATIONS 迁移表')
    rules = [
        (DB_SCHEMA_RL_OLD, DB_SCHEMA_RL_NEW, 'request_logs 建表'),
        (DB_SCHEMA_UD_OLD, DB_SCHEMA_UD_NEW, 'usage_daily 建表'),
        mig_rule,
        (DB_BUMP_SIG_OLD, DB_BUMP_SIG_NEW, 'bump_usage 签名'),
        (DB_BUMP_SQL_OLD, DB_BUMP_SQL_NEW, 'bump_usage SQL'),
    ]
    text, err = _apply(text, rules)
    if err:
        print(f'[FAIL] db.py: {err}，未落盘')
        return 1
    try:
        compile(text, path, 'exec')
    except SyntaxError as exc:
        print(f'[FAIL] db.py 补丁后语法错误，未落盘：{exc}')
        return 1
    _save(path, text)
    print('[OK] db.py 缓存列补丁已施加')
    return 0


if __name__ == '__main__':
    rc = 0
    rc |= patch_gateway(sys.argv[1] if len(sys.argv) > 1 else GW)
    rc |= patch_db(sys.argv[2] if len(sys.argv) > 2 else DB)
    sys.exit(rc)
