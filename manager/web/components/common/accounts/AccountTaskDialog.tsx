'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {Eye, Gift, Loader2, Play, Sparkles, Square, TriangleAlert} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {accountApi, errText} from '@/lib/api';
import {useI18n} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';
import {notify} from '@/lib/toast';
import {translateRunLine} from '@/lib/i18n/taskrun';
import type {Account, TaskRunStatus} from '@/lib/types';

/**
 * 单账号「活动任务」弹窗（账号页）。
 *
 * 与「任务」页的一键执行是**同一条执行路径**（都走后端 `/api/task-run`，最终跑
 * 上游 `scripts/task_runner.py`），只有 target 不同：那边是 `ALL`（全部账号），
 * 这里是**一个 uid**。所以模式的风险分级、二次确认、输出回显都照搬那边的口径：
 *
 *   · 预览   只查询，不发任何写请求（随时可点）
 *   · 领奖   只把已完成任务的奖励领回来，幂等（不伪造行为）
 *   · 做任务 点亮 + 领奖 —— **会伪造活跃上报**（造画布、连发对话、批量用专家），
 *            所以必须二次确认，且默认不选中。
 *
 * 为什么要按账号做：全量一轮的耗时随账号数**线性增长**（脚本每个写动作间隔≥1s，
 * 全量约 40 个动作 → 6 个号约 4 分钟），而实际需求往往是「这个号想单独补一轮」。
 * 为它把全部账号再跑一遍，既慢又平白多出几十次写请求（风控面也更大）。
 *
 * 后端同一时刻只允许一个任务（taskrun 是进程内单例），所以这里必须能回答
 * 「现在跑的是本账号还是别人（或全量）」——否则用户会以为自己的点击没生效。
 */
export function AccountTaskDialog({
  account,
  open,
  onOpenChange,
  onFinished,
}: {
  /** null = 未选中任何账号（弹窗关闭态） */
  account: Account | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 一次任务跑完时通知外部（积分/状态可能变了，账号列表要重拉） */
  onFinished?: () => void;
}) {
  const {t, tp} = useI18n();
  const [status, setStatus] = useState<TaskRunStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmFull, setConfirmFull] = useState(false);
  // 轮询定时器：跑的时候高频，闲着的时候低频（与 TaskRunnerPanel 同一策略）
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** 上一次是否在跑 —— 用来识别「刚刚跑完」这一刻，通知外部刷新一次 */
  const wasRunning = useRef(false);

  const load = useCallback(async () => {
    try {
      const s = await accountApi.taskRunStatus();
      setStatus(s);
      return s;
    } catch {
      return null; // 拉状态失败不打扰用户（弹窗里的按钮会自然处于不可点状态）
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    wasRunning.current = false;
    setConfirmFull(false);
    const tick = async () => {
      const s = await load();
      if (!alive) return;
      const nowRunning = s?.running === true;
      // 由「跑着」变成「不跑」：跑完了。积分可能变了，让外层重拉一次账号列表。
      if (wasRunning.current && !nowRunning) onFinished?.();
      wasRunning.current = nowRunning;
      timer.current = setTimeout(tick, nowRunning ? 2000 : 15000);
    };
    tick();
    return () => {
      alive = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [open, load, onFinished]);

  async function run(mode: 'preview' | 'claim' | 'full', confirm = false) {
    if (!account) return;
    setBusy(true);
    try {
      // target 传完整 uid：上游脚本按「uid 前缀」匹配 auths 文件，完整 uid 必然
      // 唯一命中该账号（传前 8 位在极端情况下才需要担心碰撞）。
      await accountApi.taskRunStart(mode, account.uid, confirm);
      notify.ok(t('tasks.runStarted'), t(`tasks.runMode_${mode}`));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    try {
      const r = await accountApi.taskRunStop();
      // 后端只回两种固定文案（已停止 / 当前没有正在执行的任务），走短语表
      (r.ok ? notify.ok : notify.info)(tp(r.message));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  const uid = account?.uid ?? '';
  const running = status?.running === true;
  /** 正在跑的**就是本账号**（全局单例，所以必须逐字比 target） */
  const runningThis = running && status?.target === uid;
  /** 正在跑，但跑的是别的账号或全部账号 */
  const runningOther = running && !runningThis;
  const unavailable = status !== null && !status.available;
  const otherLabel =
    (status?.target || '') === 'ALL'
      ? t('accounts.taskRunAllAccounts')
      : (status?.target || '').slice(0, 8);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="size-4 text-muted-foreground" />
            {t('accounts.taskRunTitle')}
            {runningThis && (
              <Badge variant="secondary" className="rounded-full text-[10px]">
                <Loader2 className="mr-1 size-3 animate-spin" />
                {t('tasks.runRunning', {mode: t(`tasks.runMode_${status?.mode}`)})}
              </Badge>
            )}
          </DialogTitle>
          <DialogDescription className="pt-1 text-[13px]">
            {t('accounts.taskRunFor', {name: account?.nickname || account?.uid || ''})}
          </DialogDescription>
        </DialogHeader>

        {/* 主体与操作行分家：操作行交给 DialogFooter（它自带 px-6 py-4），
            否则按钮会直接贴着弹窗底边（24px 圆角下显得很挤）。 */}
        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="space-y-3 px-6 pb-2">
            {/* 账号身份：昵称会重复（同名号更常见），uid 才是唯一标识 */}
            <div className="rounded-xl bg-muted px-3 py-2 text-[11px] text-muted-foreground">
              <div className="truncate text-foreground">
                {account?.nickname || t('accounts.unnamed')}
              </div>
              <div className="truncate font-mono">{uid}</div>
            </div>

            {confirmFull ? (
              /* 做任务前的二次确认：把「会发生什么」写清楚，再让人点。
                 与 TaskRunnerPanel 用同一套文案（tasks.runFullConfirm*）。 */
              <>
                <div className="flex items-start gap-2 rounded-xl bg-amber-500/10 px-3 py-2">
                  <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
                  <span className="text-[13px] font-medium">{t('tasks.runFullConfirmTitle')}</span>
                </div>
                <p className="text-[13px] leading-relaxed text-muted-foreground">
                  {/* 译文里用 ** 标重点（项目约定），由 RichText 渲染成 <b> */}
                  <RichText text={t('tasks.runFullConfirmBody')} />
                </p>
              </>
            ) : (
              <>
                <p className="text-[11px] leading-relaxed text-muted-foreground">
                  {t('accounts.taskRunDesc')}
                </p>

                {/* 跑的是别的任务：说清「谁在跑」，否则用户会以为自己的点击没生效 */}
                {runningOther && (
                  <div className="flex items-start gap-2 rounded-xl bg-muted px-3 py-2">
                    <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
                    <span className="text-[11px] leading-relaxed text-muted-foreground">
                      {t('accounts.taskRunBusyOther', {target: otherLabel})}
                    </span>
                  </div>
                )}

                {/* 脚本不可用时说清原因与做法（而不是给一堆点了没反应的按钮） */}
                {unavailable && (
                  <div className="flex items-start gap-2 rounded-xl bg-muted px-3 py-2">
                    <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
                    {/* whitespace-pre-line：这段说明是分条的（两种部署形态各一条修法） */}
                    <span className="text-[11px] leading-relaxed whitespace-pre-line text-muted-foreground">
                      {status?.unavailable_reason}
                    </span>
                  </div>
                )}

                {/* 输出回显：脚本按行打印进度，单账号也要能看到「跑到哪了」 */}
                {status && status.lines.length > 0 && (
                  <div className="max-h-[220px] overflow-auto rounded-xl bg-muted/60 p-3">
                    <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-foreground/80">
                      {/* 回显的是上游脚本的原始 stdout（写死中文），译文只在展示层
                          按模板逐行替换 —— 详见 lib/i18n/taskrun.ts。 */}
                      {status.lines.map((l) => translateRunLine(l)).join('\n')}
                    </pre>
                  </div>
                )}
              </>
            )}
          </div>
        </DialogBody>

        {/* 二次确认时换成「取消 / 继续」；平时是左对齐的操作条（预览 / 领奖 / 做任务） */}
        <DialogFooter className={confirmFull ? undefined : 'flex-wrap justify-start'}>
          {confirmFull ? (
            <>
              <Button variant="ghost" size="sm" className="rounded-full"
                      onClick={() => setConfirmFull(false)}>
                {t('common.cancel')}
              </Button>
              <Button size="sm" className="rounded-full" disabled={busy}
                      onClick={async () => {
                        setConfirmFull(false);
                        await run('full', true);
                      }}>
                {t('tasks.runFullConfirmOk')}
              </Button>
            </>
          ) : (
            <>
              {/* 预览：只读，无风险 */}
              <Button variant="ghost" size="sm" className="h-8 rounded-full"
                      disabled={busy || running || !!unavailable}
                      onClick={() => run('preview')}>
                <Eye className="mr-1.5 size-3.5" />
                {t('tasks.runPreview')}
              </Button>
              {/* 领奖：幂等，不伪造行为 */}
              <Button variant="ghost" size="sm" className="h-8 rounded-full"
                      disabled={busy || running || !!unavailable}
                      title={t('tasks.runClaimHint')}
                      onClick={() => run('claim')}>
                <Gift className="mr-1.5 size-3.5" />
                {t('tasks.runClaim')}
              </Button>
              {/* 做任务：会伪造上报，走二次确认 */}
              <Button variant="ghost" size="sm"
                      className="h-8 rounded-full text-amber-600 hover:text-amber-600 dark:text-amber-400"
                      disabled={busy || running || !!unavailable}
                      title={t('tasks.runFullHint')}
                      onClick={() => setConfirmFull(true)}>
                <Play className="mr-1.5 size-3.5" />
                {t('tasks.runFull')}
              </Button>
              {runningThis && (
                <Button variant="ghost" size="sm"
                        className="h-8 rounded-full text-red-500"
                        disabled={busy} onClick={stop}>
                  <Square className="mr-1.5 size-3.5" />
                  {t('tasks.runStop')}
                </Button>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
