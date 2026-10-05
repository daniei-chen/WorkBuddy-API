/**
 * 任务记录「结果」列的文案渲染（**唯一实现**）。
 *
 * 为什么单独抽一个模块：这段逻辑原先散在 `app/(main)/tasks/page.tsx` 里，是三个
 * 模板接力 + 短语表兜底 —— 只靠肉眼在浏览器里看，接错一环（`??` 写成 `||`、
 * 把 `tp()` 当取值函数传进模板、键名笔误）都会安静地渲染出键名或原文。抽出来
 * 以后可以被真实执行：`dev/i18n-pipeline-check.mjs` 用 jiti 直接 import 本模块，
 * 对五种语言逐条断言，改动即时可验。与 `lib/account-status.ts` 同一思路 ——
 * 逻辑只有一份，页面与检查脚本都走它。
 *
 * 三层接力的顺序有讲究：**先内层后外层**。一键执行历史的整行是
 * `一键做任务（ALL）退出码 0`，其中「退出码 0」自己也是个模板；若先按整行匹配，
 * 摘要就没机会重排了（它会带着中文摘要一起被塞进外层模板的 {summary}）。所以是
 * 摘要 → 外层历史模板 → 积分流水 → 服务端模板 → 短语表。
 */
import {t as tStatic, tp as tpStatic} from '@/lib/i18n';
import {translateRunSummary, translateTaskLogText} from '@/lib/i18n/tasklog';

/**
 * 一键执行的历史记录文案本地化。
 *
 * 服务端（`server/services/taskrun.py` 的 `_record_history`）按
 * `{标签}（{target}）{结果摘要}` 的模板写库：标签是固定两种，摘要则是上游脚本的
 * 英文汇总行（`task_runner done: …`）—— 原文都原样保留。这里按同一模板反解，
 * 只把外层的标签与括号交给译文重排（中文的「（）」在其它语言里并不通用），
 * 解不出就原样返回，不会把内容弄丢。
 */
const TASK_RUN_HISTORY = /^(一键领奖|一键做任务)（(.*?)）([\s\S]*)$/;
const TASK_RUN_HISTORY_KEYS: Record<string, string> = {
  一键领奖: 'tasks.runLogHistoryClaim',
  一键做任务: 'tasks.runLogHistoryFull',
};

export function taskRunHistoryText(message: string): string {
  const m = TASK_RUN_HISTORY.exec(message.trim());
  if (!m) return message;
  const key = TASK_RUN_HISTORY_KEYS[m[1]];
  if (!key) return message;
  // 摘要先本地化（`退出码 N` / `完成` 这类是后端写的固定文案）再拼进模板，
  // 否则整行带上译文标签后就再没有模板能匹配上摘要了。
  return tStatic(key, {target: m[2], summary: translateRunSummary(m[3])});
}

/**
 * 积分流水的文案本地化。
 *
 * `余额 +100（2100 → 2200）` 由服务端（`server/services/credits.py`）按模板生成并
 * 存库 —— 存的是固定句式加数字。这里按同一模板反解，交给译文重排；解不出就原样
 * 返回，不会把内容弄丢。昵称是数据，原样接在后面。
 */
const CREDIT_LEDGER = /^余额 \+(\d+)（(\d+) → (\d+)）(?:\s·\s(.*))?$/;

export function creditLedgerText(message: string): string {
  const m = CREDIT_LEDGER.exec(message.trim());
  if (!m) return message;
  const [, delta, prev, next, nickname] = m;
  return tStatic('tasks.creditLedger', {delta, prev, next}) + (nickname ? ` · ${nickname}` : '');
}

/**
 * 任务记录结果列的文案。
 *
 * 服务端已把上游英文日志转成中文（`message_cn`），这里再过一遍模板与短语表，
 * 让收录过的结果行跟随界面语言；未收录的（含自由文本的模板句）保持中文原文。
 */
export function taskLogResultText(log: {message_cn?: string; message?: string}): string {
  const raw = log.message_cn || log.message || '';
  // 模板各自带**自己的译文键**，所以不能再把 tp() 传进去当取值函数
  // （那样会拿 tasks.creditLedger 去查短语表，界面上直接显示键名）。
  const staged = creditLedgerText(taskRunHistoryText(raw));
  return translateTaskLogText(staged) ?? tpStatic(staged);
}

/**
 * 签到记录「结果」列的文案。
 *
 * 两种来源：本端触发的写 `checkin_logs.message`（固定文案，含「国际版无签到体系，
 * 已跳过」这类），上游自动签到的写 `task_logs.message_cn`（含「本轮签到完成：共 N 个…」
 * 这类模板句）。两者都是中文原文，因此先在展示层反解模板，再走短语表兜底，
 * 都认不出来时保留中文原文（宁可显示原文，也不显示键名或空白）。
 */
export function checkinResultText(log: {message?: string; success?: boolean}): string {
  const raw = log.message || '';
  return translateTaskLogText(raw)
    ?? (tpStatic(raw) || (log.success ? tStatic('common.success') : tStatic('common.failure')));
}
