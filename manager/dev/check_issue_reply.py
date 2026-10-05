"""发 issue 回复前的自检：回复是写给用户的，不是排障笔记。

用法：

    python dev/check_issue_reply.py 草稿.md                  # 检查草稿
    python dev/check_issue_reply.py 草稿.md --issue 报告.md   # 豁免引用的原文

规矩见 `docs/release-process.md` 的「issue 回复的写法：同样写给用户」。为什么要脚本：
回复挂出去就收不回来，而「对外是写给谁看的」这类错误我反复犯（#45、#46 两条回复都是
排障笔记形态）—— 靠自觉不可靠。CHANGELOG 那边是靠测试守住的，这里也钉一个。

**它只能查机械的部分**：内部标识符、测试/验收清单、检讨式叙事、篇幅。真正重要的那条
判据（「一个搜到这条 issue、不读代码的用户能否看懂」）它查不了，仍要自己过一遍。

误报的处理与 CHANGELOG 一致：**引用报告原文/界面报错**不算违规，所以 `--issue` 传入
报告的正文，出现在里面的词一律放行 —— 用户就是拿那句原文来搜的。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import NamedTuple

MAX_LINES = 20          # 结论 + 步骤通常十行内；到这里已经是「排障笔记」的体量
MAX_HEADINGS = 2        # 二级标题超过两个，基本是把回复写成了小节报告

# 用户可见的原文豁免：报告正文/界面文案里出现过的词不算违规
EXEMPT_SOURCES = ('CHANGELOG.md', 'README.md')

_URL_RE = re.compile(r'https?://\S+')

RULES: list[tuple[str, re.Pattern[str]]] = [
    # 「外部输入不能信」在这里的具体形态：函数名、模块路径、字段名、内部接口路径、提交哈希
    #
    # **环境变量不算内部标识符**：规范里明写「用户要动手的东西：环境变量、配置项、命令
    # 可以写」——把 `WB_GATEWAY_RATE_PER_MIN` 拦下来，等于逼着回复省掉用户真正需要的
    # 那一步。环境变量的书写约定是全大写（`[A-Z][A-Z0-9_]*`），而内部标识符是
    # 小写蛇形/点号路径，按形状区分得开。
    #
    # 残留风险：内部的全大写常量（如 RATE_MAX_PER_MIN）会因此放行。代价可接受——
    # 它极少出现在回复里，且那也不是这套规矩要治的毛病；反过来误拦环境变量的代价
    # 更实在（用户拿不到配置步骤）。真实靠这条守卫拦下的依然是小写标识符。
    ('内部标识符', re.compile(
        r'`[^`\n]*\(\)`'                                  # 反引号里的调用：`foo()`
        r'|`(?![A-Z][A-Z0-9_]{3,}`)[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+`'
                                                            # 反引号里的 snake_case：`_scope_models`
                                                            # （同上：`WB_XXX` 这类环境变量除外）
        r'|`[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+`'      # 反引号里的点号路径：`keysvc.model_allowed`
        r'|`[^`\n]*/[^`\n]*\.(?:py|go|ts|tsx|json)(?![a-z])`'  # 反引号里的代码文件路径
        r'|`(?:GET|POST|PUT|PATCH|DELETE)\s+/api/[^`\n]*`'  # 反引号里的内部接口路径
        r'|\b(?:GET|POST|PUT|PATCH|DELETE)\s+/api/[a-z0-9/_-]+'   # 没加反引号的同上
        r'|\b[0-9a-f]{7,40}\b'                             # 提交哈希（真实英文词不含这种形态）
        r'|\bcommit\s+[0-9a-f]{4,}\b'
        r'|[\w/\\]+\.(?:py|go|ts|tsx)\b'                    # 裸代码文件名：server/main.py
        r'|\b(?![A-Z][A-Z0-9_]{3,}\b)[A-Za-z_]\w*_\w+\b'    # 裸 snake_case：key['realm'] /
                                                            # not is_model_list / prompt_cache_key
                                                            # （全大写的环境变量除外）
        r'|\b[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\b'   # 裸点号路径 a.b.c
    )),
    ('测试/验收清单', re.compile(
        r'单测|回归测试|测试用例|用例变红|反证|断言|'
        r'全量\s*\d+\s*条|\d+\s*条(?:全部)?(?:通过|变红|全过|失败)|'
        r'验收\s*\d*\s*项|浏览器验收|e2e|全绿|全过|'
        r'ALL CHECKS PASSED|^\s*验证[:：]'
    , re.M)),
    ('检讨式叙事', re.compile(
        r'我引入|我上一轮|我错|我当时|我自己|我特意|自己踩|差点|'
        r'踩的坑|一个坑|记一下|顺便记|自审|反向验证|图省事|'
        r'是[^，。]{0,12}发现(?:的|出来的)'
    )),
    ('排障过程叙事', re.compile(
        r'第一版|起初|原实现|原来的?代码|改法|两处各写一遍|'
        r'这个顺序|我在调用侧'
    )),
    # 客套与模板腔：读起来像客服机器人，不像维护者本人。
    # 这条是维护者的直接反馈（「回复口吻不要太 ai」）——「感谢您的反馈」「如有疑问
    # 随时联系」这种句式对用户没有任何信息量，还会把真话稀释掉。
    ('客套/模板腔', re.compile(
        r'感谢(?:您|你)的?(?:反馈|报告|建议|支持)|非常感谢|十分感谢|'
        r'希望[^。]{0,10}(?:您|你)|如有(?:任何)?(?:问题|疑问)|随时(?:联系|沟通|回复)|'
        r'需要注意的是|值得一提的是|总而言之|总的来说|综上所述|'
        r'给您带来不便|敬请谅解|烦请|请知悉|'
        r'首先[^。]{0,20}其次'
    )),
]


class Finding(NamedTuple):
    line: int
    rule: str
    token: str
    text: str


def _strip_urls(text: str) -> str:
    """URL 里必然带点号和斜杠，不先摘掉会把域名判成模块路径。"""
    return _URL_RE.sub(lambda m: ' ' * len(m.group(0)), text)


def _exempt_text(issue_text: str, root: Path) -> str:
    """豁免文本：报告原文 + 用户可见的文档。

    判定是「**这个词本身**出现在豁免文本里」（界面报错原文、报告里自己写过的名字），
    而不是反过来。第一版写成 `frag in tok`，README 里的普通英文词（`model`）就把
    `_scope_models` 整条豁免掉了 —— 一个只会放行的检查比没有检查更糟。
    """
    parts = [issue_text]
    for name in EXEMPT_SOURCES:
        path = root / name
        if path.exists():
            parts.append(path.read_text(encoding='utf-8'))
    return '\n'.join(parts)


def check(text: str, *, issue_text: str = '', root: Path | None = None) -> list[Finding]:
    """返回违规清单（空列表 = 通过机械检查）。"""
    root = root or Path(__file__).resolve().parents[1]
    exempt = _exempt_text(issue_text, root)
    findings: list[Finding] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_urls(raw)
        for rule, pat in RULES:
            for m in pat.finditer(line):
                tok = m.group(0)
                # 比对豁免文本时要去掉 markdown 的反引号：报告里贴的原文通常不带
                # 反引号，而我们认出来的记号往往自带一对，直接比会永远豁免不掉。
                if tok.strip('`').strip() in exempt:
                    continue
                if any(f.line == lineno and f.rule == rule and f.token == tok
                       for f in findings):
                    continue
                findings.append(Finding(lineno, rule, tok, raw.strip()[:70]))
    return findings


def check_shape(text: str) -> list[Finding]:
    """篇幅检查：与内容无关，单独一条（超长的回复本身就违背判据）。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    heads = [ln for ln in lines if re.match(r'^#{1,3}\s', ln)]
    out: list[Finding] = []
    if len(lines) > MAX_LINES:
        out.append(Finding(len(lines), '篇幅', f'{len(lines)} 行', f'超过 {MAX_LINES} 行'))
    if len(heads) > MAX_HEADINGS:
        out.append(Finding(0, '篇幅', f'{len(heads)} 个小标题',
                           '回复不是文档，不需要分节'))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='issue 回复的机械自检')
    ap.add_argument('draft', type=Path, help='回复草稿（markdown 文件，或 - 读 stdin）')
    ap.add_argument('--issue', type=Path, default=None, help='报告原文（引用的部分豁免）')
    args = ap.parse_args(argv)

    text = sys.stdin.read() if str(args.draft) == '-' else args.draft.read_text(encoding='utf-8')
    issue_text = args.issue.read_text(encoding='utf-8') if args.issue else ''
    if not text.strip():
        print('草稿是空的 —— 检查空文件会「通过」，所以这里直接报错', file=sys.stderr)
        return 2

    findings = check(text, issue_text=issue_text) + check_shape(text)
    if not findings:
        print(f'通过机械检查（{len([l for l in text.splitlines() if l.strip()])} 行）。'
              f'仍要自己按判据过一遍：搜到这条 issue 的用户看不看得懂。')
        return 0

    print(f'发现 {len(findings)} 处「写给同事」的痕迹：\n')
    for f in findings:
        loc = f'第 {f.line} 行' if f.line else '整体'
        print(f'  {loc} [{f.rule}] {f.token}\n      {f.text}')
    print('\n改法见 docs/release-process.md 的「issue 回复的写法」：'
          '结论 + 版本 + 用户要做什么，内部的东西留给提交信息。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
