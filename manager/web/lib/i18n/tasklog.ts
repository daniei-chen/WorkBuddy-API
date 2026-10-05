/**
 * 「任务记录 → 结果列」的本地化（多语言补漏）。
 *
 * 这一列展示的是**服务端落库的结果文案**（`server/services/tasklog.py`、
 * `taskrun.py`、`accounts.py`、`tencent.py`），库里存的一直是中文原文 ——
 * 原文要留着跟上游日志对照，所以本地化只发生在**展示层**：这里按模板反解，
 * 命中就把固定措辞换成当前语言，数字、账号、异常文本这类**数据**原样保留。
 *
 * 三点与前两处（`taskrun.ts` / `tasks/page.tsx`）保持一致：
 *   · 只匹配**完整一行**（^…$）：结果文本里可能混着自由文本，宽松匹配有把
 *     行内普通内容当模板吞掉的风险；
 *   · 未命中返回 null，由调用方退到短语表兜底 —— 新文案最坏是继续显示中文
 *     原文，不会出现空行、键名或丢内容；
 *   · 简体中文（源语言）下译文模板即服务端原文，输出与改造前逐字一致。
 */
import {t as tStatic, tp as tpStatic} from './index';

/** 结果行规则：命中即按当前语言重排，未命中返回 null。 */
type ResultRule = [RegExp, (m: RegExpMatchArray) => string];

/**
 * 「阶段名: 详情」的失败行（`server/services/tasklog.py` 的 `_STAGE_LABELS`）。
 * 阶段名与详情都各有一条短语表译文，这里只负责按当前语言的标点把两半拼回去。
 */
const STAGE_LABELS =
  '刷新令牌失败|获取 Buddy 信息失败|签署协议失败|领养失败|派出失败|查询旅行状态失败|领奖失败|保存令牌失败';

const RULES: ResultRule[] = [
  // ── 脚本类任务（开学季 / 夜猫）：`执行成功（<脚本>）` / `执行失败（<详情>）` ──
  [/^执行成功（([\s\S]+)）$/, (m) => tStatic('tasks.runResultOk', {script: m[1]})],
  [/^执行失败（([\s\S]+)）$/, (m) => tStatic('tasks.runResultFail', {msg: m[1]})],

  // ── 计划阶段跳过：理由来自上游，是数据，原样保留 ──────────────────
  [/^计划任务未执行[:：]\s*([\s\S]+)$/, (m) => tStatic('tasks.runScheduledSkip', {reason: m[1]})],

  // ── 每轮签到汇总（不属于任何账号，界面在签到记录里对账用）──────────
  [
    /^本轮签到完成：共 (.*?) 个，成功 (.*?)，已签到 (.*?)，失败 (.*?)，跳过 (.*?)$/,
    (m) =>
      tStatic('tasks.runCheckinSummary', {
        total: m[1],
        ok: m[2],
        already: m[3],
        fail: m[4],
        skipped: m[5],
      }),
  ],

  // ── 签到失败明细（`accounts.py` / `tencent.py`）────────────────────
  // 异常文本是英文技术信息，原样保留，只把固定前缀换掉。
  [/^签到返回 code=(\S+)$/, (m) => tStatic('tasks.checkinCodeReturn', {code: m[1]})],
  [/^签到异常[:：]\s*([\s\S]+)$/, (m) => tStatic('tasks.checkinException', {exc: m[1]})],
  [/^读取失败[:：]\s*([\s\S]+)$/, (m) => tStatic('tasks.checkinReadFailed', {exc: m[1]})],

  // ── 「阶段名: 详情」的失败行（刷新令牌 / 领养 / 派出 …）────────────
  [
    new RegExp(`^(${STAGE_LABELS})[:：]\\s*([\\s\\S]+)$`),
    (m) => tStatic('tasks.stageLine', {label: tpStatic(m[1]), detail: tpStatic(m[2])}),
  ],
];

/**
 * 结果文案本地化：是已知模板就返回译文，否则返回 null（调用方退到短语表）。
 */
export function translateTaskLogText(message: string): string | null {
  const body = (message || '').trim();
  if (!body) return null;
  for (const [pattern, build] of RULES) {
    const hit = pattern.exec(body);
    if (hit) return build(hit);
  }
  return null;
}

/**
 * 「一键执行」历史里的**结果摘要**（`server/services/taskrun.py` 的 `_summarize`）。
 *
 * 摘要会被拼进「一键做任务（ALL）{摘要}」的外层模板里，所以必须在拼接前先重排，
 * 否则整行已经带上了译文标签，外层模板就再也匹配不上了。没有模板可用
 * （脚本自己打出来的 `task_runner done: …`）时退回短语表，仍是原样。
 */
export function translateRunSummary(summary: string): string {
  const body = (summary || '').trim();
  const m = /^退出码 (\S+)$/.exec(body);
  if (m) return tStatic('tasks.runExitCode', {code: m[1]});
  const f = /^失败[:：]\s*([\s\S]+)$/.exec(body);
  if (f) return tStatic('tasks.runFailed', {msg: f[1]});
  // `完成` / `超时终止` 是固定文案，走短语表
  return tpStatic(body);
}
