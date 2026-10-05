'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  DownloadCloud,
  Loader2,
  Pin,
  RefreshCw,
  RotateCcw,
  Server,
  ShieldCheck,
  ShieldOff,
  Sparkles,
  Terminal,
  Trash2,
  X,
  XCircle,
} from 'lucide-react';
import {errText, systemApi} from '@/lib/api';
import type {UpdateCheck, UpdateStatus} from '@/lib/types';
import {notify} from '@/lib/toast';
import {fmtAgo} from '@/lib/format';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {RichText} from '@/lib/i18n/rich-text';
import {useT} from '@/lib/i18n/provider';

/** 更新对象说明（文案走 i18n 键），用于确认弹窗与按钮文案 */
const TARGETS = [
  {
    id: 'both' as const,
    labelKey: 'updatePanel.targetBoth',
    descKey: 'updatePanel.targetBothDesc',
    hintKey: 'updatePanel.targetBothHint',
  },
  {
    id: 'upstream' as const,
    labelKey: 'updatePanel.targetUpstream',
    descKey: 'updatePanel.targetUpstreamDesc',
    hintKey: 'updatePanel.targetUpstreamHint',
  },
  {
    id: 'manager' as const,
    labelKey: 'updatePanel.targetManager',
    descKey: 'updatePanel.targetManagerDesc',
    hintKey: 'updatePanel.targetManagerHint',
  },
];

/** 把检测结果拼成一句可读摘要，用于提醒 */
function describeAvailable(c: UpdateCheck, t: (key: string, params?: Record<string, string | number>) => string): string {
  const parts: string[] = [];
  if (c.manager.has_update) parts.push(t('update.managerVersion', {v: c.manager.latest}));
  if (c.upstream.has_update) parts.push(t('update.upstreamVersion', {v: c.upstream.latest}));
  return parts.join(' · ');
}

export function UpdatePanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<{manager: string; upstream_connected: boolean; upstream_accounts: number | null} | null>(null);
  const [check, setCheck] = useState<UpdateCheck | null>(null);
  const [checking, setChecking] = useState(false);
  /**
   * 结果横幅是否已被关闭。用「是否正在运行」的边沿来复位：
   * 状态从 running 变为结束、或再次发起更新时会重新出现，
   * 不依赖 finished_at 是否存在，避免时间戳缺失时关不掉。
   */
  const [resultDismissed, setResultDismissed] = useState(false);
  /** 清除上次更新记录（issue #105）在途状态 */
  const [clearBusy, setClearBusy] = useState(false);
  /** 上游版本固定输入（空 = 跟随分支） */
  const [refInput, setRefInput] = useState('');
  const [refBusy, setRefBusy] = useState(false);
  const refInited = useRef(false);
  const logRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const [st, vs, ck] = await Promise.allSettled([
        systemApi.updateStatus(),
        systemApi.versions(),
        systemApi.checkUpdate(),
      ]);
      if (st.status === 'fulfilled') {
        setStatus(st.value);
        if (!refInited.current) {
          refInited.current = true;
          setRefInput(st.value.upstream_ref || '');
        }
      }
      if (vs.status === 'fulfilled') setVersions(vs.value);
      if (ck.status === 'fulfilled') setCheck(ck.value);
    } catch {
      /* 更新期间服务重启，忽略瞬时失败 */
    }
  }, []);

  /** 强制重新检测（绕过服务端 6 小时缓存） */
  const recheck = useCallback(async () => {
    setChecking(true);
    try {
      const r = await systemApi.checkUpdate(true);
      setCheck(r);
      if (r.has_any) {
        notify.warn(t('updatePanel.newVersion'), describeAvailable(r, t));
      } else {
        notify.ok(t('updatePanel.upToDate'), t('updatePanel.upToDateDesc'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setChecking(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  // 运行中高频轮询，空闲时低频（服务可能在更新中短暂不可用）
  const running = !!status?.running;
  useEffect(() => {
    const interval = running ? 1500 : 15000;
    const timer = window.setInterval(() => {
      void load();
    }, interval);
    return () => window.clearInterval(timer);
  }, [load, running]);

  // running 一旦变为 true，就复位「已关闭」，使本轮结果在结束后重新可见
  useEffect(() => {
    if (running) setResultDismissed(false);
  }, [running]);

  // 日志自动滚到底
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [status?.logs]);

  const logs = useMemo(() => status?.logs ?? [], [status?.logs]);
  const logText = useMemo(
    () => (logs.length ? logs.map((l) => l.text).join('\n') : status?.log_tail || ''),
    [logs, status?.log_tail],
  );
  /** 上一次更新已经结束（成功或失败）——有结果可清除 */
  const hasResult = !!status && !status.running && status.ok !== null;

  /**
   * 清除上次更新的结果与日志（issue #105）。
   *
   * 此前只能靠「再发起一次更新」覆盖：状态文件与 update.log 都留着，那条
   * 「更新未完成」和日志永远擦不掉。更新进行中后端会拒绝（409），这里也把入口收起来。
   */
  async function clearResult() {
    setClearBusy(true);
    try {
      await systemApi.clearUpdateStatus();
      notify.ok(t('updatePanel.cleared'));
      setResultDismissed(false);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setClearBusy(false);
    }
  }

  async function start(target: 'manager' | 'upstream' | 'both') {
    setBusy(true);
    try {
      const r = await systemApi.startUpdate(target);
      notify.ok(t('updatePanel.updateStarted'), r.message);
      // 立即拉一次，进入高频轮询
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  /** 保存上游版本固定（空 = 取消固定） */
  async function saveUpstreamRef() {
    setRefBusy(true);
    try {
      const r = await systemApi.setUpstreamRef(refInput.trim());
      setRefInput(r.upstream_ref || '');
      if (r.upstream_ref) {
        notify.ok(
          t('updatePanel.refPinned'),
          t('updatePanel.refPinnedDesc', {ref: r.upstream_ref}),
        );
      } else {
        notify.info(t('updatePanel.refUnpinned'), t('updatePanel.refUnpinnedDesc'));
      }
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRefBusy(false);
    }
  }

  const done = !!status && !status.running && status.ok !== null && !resultDismissed;

  return (
    <div className="space-y-4">
      {/* 当前版本 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            {t('updatePanel.currentVersion')}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={recheck}
              disabled={checking || running}
            >
              <RefreshCw className={checking ? 'animate-spin' : ''} />
              {t('updatePanel.checkUpdate')}
            </Button>
            <Button variant="outline" size="sm" className="rounded-full" onClick={load} disabled={running}>
              <RefreshCw className={running ? 'animate-spin' : ''} />
              {t('common.refresh')}
            </Button>
          </div>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.manager')}</div>
            <div className="mt-1 flex items-center gap-1.5 text-sm font-semibold tabular-nums">
              {versions?.manager || status?.version || '—'}
              {/* 供应链防护：本次更新的包是否经过签名校验，必须让用户看得见 */}
              {status?.signature?.status === 'verified' && (
                <span
                  className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 dark:text-emerald-400"
                  title={
                    status.signature.detail
                      ? t('updatePanel.signedTitleDetail', {detail: status.signature.detail})
                      : t('updatePanel.signedTitle')
                  }
                >
                  <ShieldCheck className="h-3 w-3" />
                  {t('updatePanel.signed')}
                </span>
              )}
              {status?.signature?.status === 'skipped' && (
                <span
                  className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-400"
                  title={t('updatePanel.unsignedTitle')}
                >
                  <ShieldOff className="h-3 w-3" />
                  {t('updatePanel.unsigned')}
                </span>
              )}
            </div>
          </div>
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.upstreamConn')}</div>
            <div className="mt-1 flex items-center gap-1.5 text-sm font-semibold">
              {versions?.upstream_connected ? (
                <>
                  <span className="text-emerald-600 dark:text-emerald-400">{t('updatePanel.connOk')}</span>
                  {typeof versions.upstream_accounts === 'number' && (
                    <span className="text-xs font-normal text-muted-foreground">
                      {t('updatePanel.accountCount', {
                        count: versions.upstream_accounts,
                        n: versions.upstream_accounts,
                      })}
                    </span>
                  )}
                </>
              ) : (
                <span className="text-red-600 dark:text-red-400">{t('updatePanel.connDown')}</span>
              )}
            </div>
          </div>
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.lastUpdate')}</div>
            <div className="mt-1 text-sm font-semibold">
              {status?.finished_at ? fmtAgo(status.finished_at) : t('format.never')}
            </div>
          </div>
        </div>
        {status?.upstream_dir && (
          <div className="mt-2 truncate font-mono text-[11px] text-muted-foreground" title={status.upstream_dir}>
            {t('updatePanel.upstreamDir', {dir: status.upstream_dir})}
          </div>
        )}
      </div>

      {/* 新版本提醒 */}
      {check?.has_any && !status?.running && (
        <div className="rounded-[20px] border border-blue-500/40 bg-blue-500/10 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex items-start gap-2.5">
              <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-blue-500" />
              <div className="space-y-1.5">
                <div className="text-xs font-medium">{t('updatePanel.newVersionAvailable')}</div>
                {check.manager.has_update && (
                  <div className="text-[11px] text-muted-foreground">
                    {t('updatePanel.managerUpgrade', {from: check.manager.current})}{' '}
                    <span className="font-medium text-foreground">{check.manager.latest}</span>
                  </div>
                )}
                {/* 签名未就绪：发布后到维护者签名之间有个窗口期，这时更新一定会被
                    拒绝。先说清楚、并把按钮收起来，比让人点下去看到「已拒绝安装」好
                    （issue #127/#129）。签名上传后最迟五分钟、或点一次刷新就会恢复。 */}
                {check.manager.has_update && check.manager.sig_ready === false && (
                  <div className="flex items-start gap-1.5 text-[11px] text-amber-600 dark:text-amber-400">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span>{t('updatePanel.sigPending')}</span>
                  </div>
                )}
                {check.upstream.has_update && (
                  <div className="space-y-1 text-[11px] text-muted-foreground">
                    <div>
                      {t('updatePanel.upstreamUpgrade', {from: check.upstream.current || t('metric.unknown')})}{' '}
                      <span className="font-medium text-foreground">{check.upstream.latest}</span>
                      {!!check.upstream.ahead && (
                        <span className="ml-1">{t('updatePanel.commitsBehind', {n: check.upstream.ahead})}</span>
                      )}
                    </div>
                    {/* 变更列表：上游常一次累积多个提交，列出各自说明才能判断
                        「这批更新做了什么、值不值得跟」 */}
                    {check.upstream.changes?.length ? (
                      <details className="group">
                        <summary className="cursor-pointer list-none">
                          <span className="text-foreground/80 group-open:hidden">
                            {t('updatePanel.viewCommits', {
                              n: check.upstream.ahead || check.upstream.changes.length,
                            })}
                          </span>
                          <span className="hidden text-foreground/80 group-open:inline">
                            {t('updatePanel.hideCommits')}
                          </span>
                        </summary>
                        <ul className="mt-1 max-h-[220px] space-y-0.5 overflow-auto rounded-xl bg-background/60 p-2">
                          {check.upstream.changes.map((c) => (
                            <li key={c.sha} className="flex gap-2">
                              <span className="shrink-0 font-mono text-[10px] text-muted-foreground/70">
                                {c.sha}
                              </span>
                              <span className="min-w-0 flex-1 break-all">{c.subject}</span>
                            </li>
                          ))}
                          {check.upstream.truncated && (
                            <li className="pt-0.5 text-[10px] text-muted-foreground/70">
                              {t('updatePanel.changesTruncated', {n: check.upstream.changes.length})}
                            </li>
                          )}
                        </ul>
                      </details>
                    ) : (
                      check.upstream.subject && (
                        <div className="break-all">
                          {t('updatePanel.latestCommit', {subject: check.upstream.subject})}
                          {check.upstream.date && `（${check.upstream.date.slice(0, 10)}）`}
                        </div>
                      )
                    )}
                  </div>
                )}
                <div className="text-[11px] text-muted-foreground">
                  {t('updatePanel.updateHint')}
                </div>
              </div>
            </div>
            <div className="flex gap-2">
              {check.upstream.has_update && (
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-full"
                  disabled={!isAdmin || busy || running}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      const r = await systemApi.startUpdate('upstream');
                      notify.ok(t('updatePanel.updateStarted'), r.message);
                      await load();
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  {t('updatePanel.updateUpstream')}
                </Button>
              )}
              {check.manager.has_update && (
                <Button
                  size="sm"
                  className="rounded-full"
                  // 签名未就绪时不放行：这条路必然以「已拒绝安装」收场，让用户点了
                  // 再失败只是把同一条信息换个地方说（见上方提示）。
                  disabled={!isAdmin || busy || running || check.manager.sig_ready === false}
                  title={check.manager.sig_ready === false ? t('updatePanel.sigPending') : undefined}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      const r = await systemApi.startUpdate('manager');
                      notify.ok(t('updatePanel.updateStarted'), r.message);
                      await load();
                    } catch (e) {
                      notify.err(errText(e));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <DownloadCloud />
                  {t('updatePanel.updateManager')}
                </Button>
              )}
            </div>
          </div>
        </div>
      )}

      {check && !check.has_any && !check.manager.error && !check.upstream.error && !status?.running && (
        <div className="flex items-center gap-2 rounded-[20px] border border-emerald-500/30 bg-emerald-500/10 px-4 py-3">
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
          <span className="text-xs">{t('updatePanel.allUpToDate')}</span>
          {check.checked_at > 0 && (
            <span className="text-[11px] text-muted-foreground">
              {t('updatePanel.checkedAgo', {ago: fmtAgo(check.checked_at)})}
            </span>
          )}
        </div>
      )}

      {/* 更新状态 */}
      {status?.running && (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-blue-500/30 bg-blue-500/10 p-4">
          <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-blue-500" />
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {t('updatePanel.updatingNow', {step: status.step || t('updatePanel.stepRunning')})}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {t('updatePanel.updatingHint')}
            </div>
          </div>
        </div>
      )}

      {done && (
        <div
          className={
            'flex items-start gap-2.5 rounded-[20px] border p-4 ' +
            (status?.ok
              ? 'border-emerald-500/30 bg-emerald-500/10'
              : 'border-red-500/30 bg-red-500/10')
          }
        >
          {status?.ok ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
          ) : (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
          )}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {status?.ok ? t('updatePanel.updateDone') : t('updatePanel.updateNotDone')}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {status?.ok
                ? (status?.duration ? t('updatePanel.doneDuration', {n: status.duration}) : '') +
                  t('updatePanel.doneRestart') +
                  (status?.signature?.status === 'verified' ? t('updatePanel.doneSigned') : '')
                : t('updatePanel.failedDetail')}
            </div>
            {status?.ok && status?.signature?.status === 'skipped' && (
              <div className="flex items-start gap-1.5 pt-1 text-[11px] text-amber-600 dark:text-amber-400">
                <ShieldOff className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  <RichText text={t('updatePanel.skippedWarn')} />
                </span>
              </div>
            )}
          </div>
          <button
            type="button"
            aria-label={t('updatePanel.closeTip')}
            title={t('updatePanel.closeTip')}
            className="-m-1 shrink-0 rounded-full p-1 text-muted-foreground transition-colors hover:bg-foreground/10 hover:text-foreground"
            onClick={() => setResultDismissed(true)}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {status && status.updater_found === false && (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div className="space-y-1">
            <div className="text-xs font-medium">{t('updatePanel.updaterMissing')}</div>
            <div className="text-[11px] text-muted-foreground">
              <RichText text={t('updatePanel.updaterMissingHint')} />
            </div>
          </div>
        </div>
      )}

      {/* 更新操作 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 text-sm font-medium">{t('updatePanel.oneClickUpdate')}</div>
        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
          {t('updatePanel.oneClickUpdateDesc')}
        </div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {TARGETS.map((target, i) => {
            // 能否更新上游取决于**能否操作 docker**（后端按实际能力判定），
            // 而不是"是否在容器里"：容器挂了 docker.sock 就能做这些事。
            // 不可用时直接禁用并说明替代做法，而不是让用户点了才失败。
            const blocked = status?.can_update_upstream === false && target.id !== 'manager';
            const text = blocked
              ? t('updatePanel.dockerBlocked')
              : t('updatePanel.targetDescText', {
                  desc: t(target.descKey),
                  hint: t(target.hintKey),
                });
            return (
            <ConfirmDialog
              key={target.id}
              title={t('updatePanel.targetConfirmTitle', {label: t(target.labelKey)})}
              description={text}
              confirmText={t('updatePanel.startUpdate')}
              onConfirm={() => start(target.id)}
              trigger={
                <button
                  type="button"
                  disabled={!isAdmin || busy || running || blocked}
                  title={blocked ? t('updatePanel.targetBlockedTitle') : undefined}
                  className={
                    'flex flex-col items-start gap-1 rounded-2xl px-3.5 py-3 text-left transition-colors ' +
                    'bg-background/60 hover:bg-background disabled:cursor-not-allowed disabled:opacity-50'
                  }
                >
                  <div className="flex items-center gap-1.5 text-xs font-medium">
                    {i === 0 ? <DownloadCloud className="h-3.5 w-3.5" /> : <RotateCcw className="h-3.5 w-3.5" />}
                    {t(target.labelKey)}
                  </div>
                  <div className="text-[11px] leading-4 text-muted-foreground">{t(target.descKey)}</div>
                </button>
              }
            />
            );
          })}
        </div>
        {status?.can_update_upstream === false && (
          <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('updatePanel.dockerNote')} />
          </p>
        )}
        {!isAdmin && (
          <p className="mt-2 text-[11px] text-muted-foreground">{t('updatePanel.readonlyNote')}</p>
        )}
      </div>

      {/* 上游版本固定：上游某个提交自身有问题时，固定回上一个可用提交 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 flex flex-wrap items-center gap-2 text-sm font-medium">
          <Pin className="h-4 w-4" />
          {t('updatePanel.pinTitle')}
          {status?.upstream_ref ? (
            <Badge variant="secondary" className="rounded-full text-amber-600 dark:text-amber-400">
              {t('updatePanel.pinPinned', {ref: status.upstream_ref})}
            </Badge>
          ) : (
            <Badge variant="secondary" className="rounded-full text-muted-foreground">
              {t('updatePanel.pinFollowing')}
            </Badge>
          )}
        </div>
        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
          <RichText text={t('updatePanel.pinDesc')} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={refInput}
            disabled={!isAdmin || refBusy}
            placeholder={t('updatePanel.pinPlaceholder')}
            className="h-8 w-full max-w-[320px] bg-background font-mono text-xs"
            onChange={(e) => setRefInput(e.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            className="rounded-full"
            disabled={!isAdmin || refBusy}
            onClick={saveUpstreamRef}
          >
            {refBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Pin className="h-3.5 w-3.5" />}
            {t('common.save')}
          </Button>
          {status?.upstream_ref && (
            <Button
              size="sm"
              variant="ghost"
              className="rounded-full text-muted-foreground"
              disabled={!isAdmin || refBusy}
              onClick={async () => {
                setRefInput('');
                setRefBusy(true);
                try {
                  await systemApi.setUpstreamRef('');
                  notify.info(t('updatePanel.refUnpinned'), t('updatePanel.refUnpinnedDesc'));
                  await load();
                } catch (e) {
                  notify.err(errText(e));
                } finally {
                  setRefBusy(false);
                }
              }}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              {t('updatePanel.unpin')}
            </Button>
          )}
        </div>
      </div>

      {/* 日志。**没有可看的内容时不渲染这块**（issue #105：一次失败会长期驻留，
          清掉记录后这里还应整块消失，而不是永远挂着一个空框）。 */}
      {(logText || running || hasResult) && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Terminal className="h-4 w-4" />
              {t('updatePanel.logTitle')}
            </div>
            <div className="flex items-center gap-2">
              {logs.length > 0 && (
                <Badge variant="secondary" className="rounded-full text-[10px]">
                  {t('updatePanel.logLines', {count: logs.length, n: logs.length})}
                </Badge>
              )}
              {/* 清除上次更新的结果与日志。更新进行中不给入口（后端也会拒绝）。 */}
              {!running && (logText || hasResult) && (
                <ConfirmDialog
                  title={t('updatePanel.clearResultTitle')}
                  description={t('updatePanel.clearResultDesc')}
                  confirmText={t('updatePanel.clearResult')}
                  destructive
                  onConfirm={clearResult}
                  trigger={
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 rounded-full px-2 text-[11px] text-muted-foreground"
                      disabled={clearBusy}
                    >
                      <Trash2 className="h-3 w-3" />
                      {t('updatePanel.clearResult')}
                    </Button>
                  }
                />
              )}
            </div>
          </div>
          {logText ? (
            <div
              ref={logRef}
              className="scroll-slim max-h-[320px] overflow-auto rounded-2xl bg-background/60 p-3"
            >
              <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-muted-foreground">
                {logText}
              </pre>
            </div>
          ) : (
            <div className="rounded-2xl bg-background/60 px-3 py-8 text-center text-xs text-muted-foreground">
              {t('updatePanel.logEmpty')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
