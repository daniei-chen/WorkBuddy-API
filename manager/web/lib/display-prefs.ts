/**
 * 界面显示偏好：存在**浏览器本地**（localStorage），不落后端。
 *
 * 为什么不像上游配置那样存服务端：这一项决定的是「数字怎么摆」，不是系统的行为，
 * 每个使用者可以有自己的偏好——与语言、版本切换同类（它们的先例见
 * lib/i18n/config.ts 与 lib/realm-context.tsx）。
 *
 * 读不到时一律返回默认值：隐私模式下 localStorage 会抛错，那不该让页面白屏。
 */
import type {CreditExpiry} from './types';

/** 仪表盘「最近到期积分」是否按天归并。 */
const EXPIRY_DAILY_KEY = 'wb.expiry-daily-group';

/**
 * 读取「到期积分按天模糊统计」开关。
 *
 * **默认关闭**（返回 false）：这是改变既有数字含义的选项——开启后仪表盘上那个
 * 数字从「最近一笔到期多少」变成「最近这一天共到期多少」，两者数量级不同。
 * 默认为关，升级后看到的还是原来那个数，想按天看的人自己打开。
 */
export function getExpiryDailyGroup(): boolean {
  try {
    return window.localStorage.getItem(EXPIRY_DAILY_KEY) === '1';
  } catch {
    return false;   // 隐私模式/被禁用：用默认值
  }
}

export function setExpiryDailyGroup(on: boolean): void {
  try {
    window.localStorage.setItem(EXPIRY_DAILY_KEY, on ? '1' : '0');
  } catch {
    /* 写不进去就算了：本次会话仍按用户的选择渲染（state 在调用方），
       只是下次打开会回到默认值 */
  }
}

/**
 * 把到期明细按**本地日历日**归并：同一天到期的多笔合并成一条。
 *
 * 为什么是本地日历日而不是「相隔 24 小时」：用户看的是日历——
 * 「9 月 30 日到期三笔」比「最早那笔 12.4 天后、其余 12.6 天后」好读得多，
 * 而且那天能采取行动（多跑任务把它们花掉）也是按日算的。
 *
 * 合并后的时刻取该日**最早**的那一笔：倒计时宁可保守。用户看到「还有 12 天」
 * 时会按 12 天安排，若实际是当天 00:00 作废，那这一天就用不上了。
 *
 * 不修改传入的数组与其中的对象（调用方可能还要用原始明细）。
 */
export function groupExpiriesByDay(expiries: CreditExpiry[]): CreditExpiry[] {
  const byDay = new Map<string, CreditExpiry>();
  for (const e of expiries) {
    const d = new Date(e.at * 1000);
    // 用本地年月日作 key。不要用 `at / 86400`：那是 UTC 日切，
    // 东八区会把当天 08:00 前的到期算进前一天。
    const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    const cur = byDay.get(key);
    if (cur) {
      cur.amount += e.amount;
      if (e.at < cur.at) cur.at = e.at;
    } else {
      // 新建对象而不是引用 e：后面要就地累加 amount，不能改到调用方的数据
      byDay.set(key, {at: e.at, amount: e.amount});
    }
  }
  return [...byDay.values()].sort((a, b) => a.at - b.at);
}
