"""更新日志（CHANGELOG.md）解析。

为什么要做：仓库里的 CHANGELOG.md 写得很细（每个版本改了什么、为什么改），
但界面上看不到——用户只能去 GitHub 翻。本地部署（尤其内网）连 GitHub 都不一定通。

这里把它解析成结构化数据交给前端渲染，**不引入 markdown 库**：
CHANGELOG 的格式是固定的三级结构（版本 → 小节 → 条目），解析出来即可，
前端的少量内联标记（粗体/行内代码）由前端做轻量处理。

发布包里本来就带 CHANGELOG.md（Release 产物含根目录文件），
所以离线环境也能看到。
"""
from __future__ import annotations

import re

from .. import config

# 版本标题：## [1.0.15] - 2026-09-13  或  ## [未发布]
_VERSION_RE = re.compile(r'^##\s+\[([^\]]+)\]\s*(?:-\s*(.+))?$')
# 小节标题：### 修复
_SECTION_RE = re.compile(r'^###\s+(.+?)\s*$')
# 条目：- xxx（缩进 2 空格为子条目）
_ITEM_RE = re.compile(r'^(\s*)-\s+(.*)$')

# 最多返回多少个版本（界面是折叠列表，太多无意义）
_MAX_VERSIONS = 30


def _candidate_paths() -> list:
    """按优先级列出可能的日志文件位置。

    为什么不止根目录：一键更新只整体替换 `server/`、`web/out/`、`deploy/`
    与 `.version`，**不替换根目录的其他文件**。历史上（v1.0.16 之前）更新器
    就是这样，导致老部署升上来后根目录根本没有 CHANGELOG.md，界面报
    「未找到更新日志文件」——而且因为已是最新版，再点更新也不会补上。
    因此发布打包时会在 `server/CHANGELOG.md` 放一份副本：`server/` 每次更新
    都会被整体替换，这条路一定拿得到最新的日志。
    """
    return [config.ROOT / 'CHANGELOG.md', config.ROOT / 'server' / 'CHANGELOG.md']


def _strip_inline(text: str) -> str:
    """去掉会干扰纯文本展示的标记，保留粗体/行内代码交给前端处理。

    这里只做最小清理：去掉行尾多余空白。真正的 `**x**` / `` `x` ``
    由前端渲染成对应元素。
    """
    return text.strip()


def parse_changelog(text: str) -> list[dict]:
    """把 CHANGELOG.md 解析成 [{version, date, unreleased, sections:[{title, items:[{level,text}]}]}]。"""
    versions: list[dict] = []
    cur: dict | None = None
    section: dict | None = None
    # 续行：上一条目的后续行（CHANGELOG 里长条目会换行并缩进）
    pending: dict | None = None

    def flush_pending() -> None:
        nonlocal pending
        pending = None

    for raw in (text or '').splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _VERSION_RE.match(line)
        if m:
            flush_pending()
            name = m.group(1).strip()
            date = (m.group(2) or '').strip()
            cur = {
                'version': name,
                'date': date,
                'unreleased': name in ('未发布', 'Unreleased'),
                'sections': [],
            }
            versions.append(cur)
            section = None
            continue

        if cur is None:
            continue

        m = _SECTION_RE.match(line)
        if m:
            flush_pending()
            section = {'title': m.group(1).strip(), 'items': []}
            cur['sections'].append(section)
            continue

        m = _ITEM_RE.match(line)
        if m and section is not None:
            flush_pending()
            indent = len(m.group(1))
            item = {
                # 缩进 ≥2 视为子条目，前端用小圆点区分
                'level': 1 if indent >= 2 else 0,
                'text': _strip_inline(m.group(2)),
            }
            section['items'].append(item)
            pending = item
            continue

        # 普通文本行：若是上一条目的续行（缩进对齐），拼回去
        if pending is not None and section is not None and line.startswith('  '):
            pending['text'] = f"{pending['text']} {line.strip()}"
            continue
        # 版本间的分隔线或说明文字，忽略
        flush_pending()

    return versions


def load_changelog() -> dict:
    """读取并解析 CHANGELOG.md。读不到时返回 available=False 并说明原因。"""
    candidates = _candidate_paths()
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        tried = '、'.join(str(p) for p in candidates)
        return {
            'available': False,
            'error': f'未找到更新日志文件（已尝试：{tried}）',
            'versions': [],
        }
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except Exception as exc:  # noqa: BLE001
        return {
            'available': False,
            'error': f'读取更新日志失败（{path}）：{exc}',
            'versions': [],
        }

    versions = parse_changelog(text)
    return {
        'available': True,
        'path': str(path),
        'total': len(versions),
        'versions': versions[:_MAX_VERSIONS],
        'truncated': len(versions) > _MAX_VERSIONS,
    }
