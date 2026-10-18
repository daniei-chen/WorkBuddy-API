'use client';

import {useState} from 'react';
import {Play, Square, Eye, Gift, Sparkles, Loader2, TriangleAlert} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {useI18n} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';
import {translateRunLine} from '@/lib/i18n/taskrun';
import {useTaskRun} from '@/lib/use-task-run';

/**
 * 成长任务一键执行面板（issue #19）。
 *
 * 三种模式按**风险从低到高**排列，界面措辞必须让用户看懂区别 —— 这不是
 * 三个平级按钮：
 *
 *   · 预览   只查询，不发任何写请求（安全，随时可点）
 *   · 领奖   只把已完成任务的奖励领回来，幂等（不伪造行为）
 *   · 做任务 点亮 + 领奖 —— **会伪造活跃上报**（造画布、连发对话、批量用专家），
 *            上游自己都标注「写操作慎用」。所以它必须二次确认，且默认不选中。
 *
 * 定时只跑「领奖」：点亮绝不进定时（后端的调度器硬性只允许 claim）。
 */
export function TaskRunnerPanel() {
  const {t} = useI18n();
  const [confirmFull, setConfirmFull] = useState(false);
  const {status, busy, run, stop} = useTaskRun({target: 'ALL'});

  const running = status?.running === true;
  const unavailable = status && !status.available;

  return (
    <section className="rounded-2xl border bg-card p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Sparkles className="size-4 text-muted-foreground" />
          <span className="text-sm font-medium">{t('tasks.runTitle')}</span>
          {running && (
            <Badge variant="secondary" className="rounded-full text-[10px]">
              <Loader2 className="mr-1 size-3 animate-spin" />
              {t('tasks.runRunning', {mode: t(`tasks.runMode_${status?.mode}`)})}
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
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
          {running && (
            <Button variant="ghost" size="sm"
                    className="h-8 rounded-full text-red-500"
                    disabled={busy} onClick={stop}>
              <Square className="mr-1.5 size-3.5" />
              {t('tasks.runStop')}
            </Button>
          )}
        </div>
      </div>

      {/* 脚本不可用时说清原因与做法（而不是给一堆点了没反应的按钮） */}
      {unavailable && (
        <div className="mb-3 flex items-start gap-2 rounded-xl bg-muted px-3 py-2">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
          {/* whitespace-pre-line：这段说明是**分条的**（两种部署形态各一条修法），
              不加的话换行会被折成空格、整段挤成一坨，正好把「我该做哪一步」
              这个最有用的信息淹掉。 */}
          <span className="text-[11px] leading-relaxed whitespace-pre-line text-muted-foreground">
            {status?.unavailable_reason}
          </span>
        </div>
      )}

      <p className="mb-2 text-[11px] leading-relaxed text-muted-foreground">
        {t('tasks.runDesc')}
      </p>

      {/* 输出回显：脚本按行打印进度，等长任务需要看到「跑到哪了」 */}
      {status && status.lines.length > 0 && (
        <div className="max-h-[240px] overflow-auto rounded-xl bg-muted/60 p-3">
          <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-foreground/80">
            {/* 面板回显的是上游脚本的原始 stdout（写死中文）。译文只在展示层按模板
                逐行替换，存储与上游脚本都保持原文 —— 详见 lib/i18n/taskrun.ts。 */}
            {status.lines.map((l) => translateRunLine(l)).join('\n')}
          </pre>
        </div>
      )}

      {/* 做任务前的二次确认：把「会发生什么」写清楚，再让人点 */}
      <Dialog open={confirmFull} onOpenChange={setConfirmFull}>
        <DialogContent className="max-w-[460px]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <TriangleAlert className="size-4 text-amber-500" />
              {t('tasks.runFullConfirmTitle')}
            </DialogTitle>
            <DialogDescription className="pt-2 text-[13px] leading-relaxed">
              {/* 译文里用 ** 标重点（项目约定），由 RichText 渲染成 <b> —— 
                  直接输出会把星号原样显示出来。 */}
              <RichText text={t('tasks.runFullConfirmBody')} />
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" size="sm" className="rounded-full"
                    onClick={() => setConfirmFull(false)}>
              {t('common.cancel')}
            </Button>
            <Button size="sm" className="rounded-full"
                    disabled={busy}
                    onClick={async () => {
                      setConfirmFull(false);
                      await run('full', true);
                    }}>
              {t('tasks.runFullConfirmOk')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
