'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {Loader2, CheckCircle2, AlertTriangle, ExternalLink} from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {accountApi, errText} from '@/lib/api';
import {useRealm} from '@/lib/realm-context';
import {Button} from '@/components/ui/button';
import {CopyButton, ShareButton} from '@/components/ui/copy-button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogHeader,
} from '@/components/animate-ui/radix/dialog';

type Phase = 'loading' | 'waiting' | 'success' | 'error';

/**
 * 连续失败几次才打断轮询。
 *
 * 取 3（约 6 秒）：单次抖动/超时不该打断用户扫码，而持续失败（例如账号目录
 * 无权写入）必须让用户看到原因 —— 否则界面会一直转圈到 5 分钟后再报
 * 「二维码已失效」，把真正的故障藏起来（issue #26）。
 */
const POLL_FAIL_LIMIT = 3;

/**
 * 国际版可选地区（与后端 INTERNATIONAL_REGIONS 保持一致）。
 * 取自国际版官网的短名单；不预选，因为地区属于账号归属信息。
 * label 为 i18n 键：地区名要跟着界面语言走（代码本身是固定的 ISO 码）。
 */
const INTERNATIONAL_REGIONS = [
  {code: 'HK', key: 'region.HK'},
  {code: 'MO', key: 'region.MO'},
  {code: 'SG', key: 'region.SG'},
  {code: 'TH', key: 'region.TH'},
  {code: 'PH', key: 'region.PH'},
  {code: 'MY', key: 'region.MY'},
  {code: 'ID', key: 'region.ID'},
] as const;

/**
 * 「用户回到这个页面了」的信号源列表。
 *
 * 为什么不是只挂 visibilitychange：Chrome 对**被遮挡**的窗口（点授权链接后
 * 那扇页盖住面板就是这种情况）会把定时器节流到约 1 次/分钟，而此时
 * `document.hidden` 仍是 false、`visibilitychange` 一次都不触发 —— 实测
 * 2 秒的定时器在 64 秒里只跑了 2 次。所以还要挂在「人确实在操作」的事件上：
 * 他回来看结果必然会让窗口获得焦点、点一下或敲一下键盘，那一刻立即补一次，
 * 这些事件不受遮挡节流影响。
 *
 * 在**函数内**才去碰 window/document：模块顶层读它们会让静态导出在构建期
 * 预渲染时直接报错（本项目是 output: 'export'）。
 */
function wakeEvents(): Array<[EventTarget, string]> {
  if (typeof window === 'undefined') return [];
  return [
    [document, 'visibilitychange'],
    [window, 'focus'],
    [document, 'pointerdown'],
    [document, 'keydown'],
  ];
}

export function AddAccountDialog({
  open,
  onOpenChange,
  onSuccess,
  upstreamId,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSuccess?: () => void;
  /** 目标分组（多账号池）：新账号落进该分组的账号目录；null / 省略 = 默认分组 */
  upstreamId?: number | null;
}) {
  const t = useT();
  const [phase, setPhase] = useState<Phase>('loading');
  /** 版本跟随全站切换：切到国际版时扫码走国际版端点，并需要选地区 */
  const {realm, label: realmName} = useRealm();
  /** 国际版地区代码（如 HK）。不预选：地区属于账号归属信息，交由用户决定 */
  const [region, setRegion] = useState('');
  const [proxy, setProxy] = useState<string | undefined>(undefined);
  const [proxyRoutes, setProxyRoutes] = useState<string[]>([]);
  const [routesReady, setRoutesReady] = useState(false);
  const [routeError, setRouteError] = useState('');
  const [authUrl, setAuthUrl] = useState('');
  const [message, setMessage] = useState('');
  const stateRef = useRef('');
  const timerRef = useRef<number | null>(null);
  /** 上一次 poll 是否还在飞：单次超过 2 秒时避免请求叠加 */
  const pollingRef = useRef(false);
  /** 连续轮询失败次数：偶发抖动不该打断用户，持续失败才报出来 */
  const failsRef = useRef(0);
  /**
   * 当前这轮轮询的 tick 函数。
   *
   * 存成 ref 是为了让 `visibilitychange` 监听器能拿到最新那份 —— 与 timer
   * 同生共死，避免「切回来时调用了一个已被 stopPoll 作废的闭包」。
   */
  const tickRef = useRef<(() => void) | null>(null);

  const stopPoll = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    // 一并摘掉唤醒监听：否则弹窗关掉后，切标签页/点页面仍会去调用已作废的
    // tick（轻则白发请求，重则对着已关闭的弹窗 setState）。
    // 与 start() 里用的是同一个列表，漏摘一个就是内存泄漏 + 白发请求。
    if (tickRef.current !== null) {
      wakeEvents().forEach(([target, event]) =>
        target.removeEventListener(event, tickRef.current!),
      );
      tickRef.current = null;
    }
    pollingRef.current = false;
  }, []);

  useEffect(() => {
    if (!open) { setRoutesReady(false); return; }
    let active = true;
    setRoutesReady(false);
    setRouteError('');
    accountApi.proxies().then((data) => {
      if (!active) return;
      setProxyRoutes(data.routes);
      setProxy(data.default || '');
      setRoutesReady(true);
    }).catch((error) => {
      if (active) setRouteError(errText(error));
    });
    return () => { active = false; };
  }, [open]);

  const start = useCallback(async () => {
    stopPoll();
    failsRef.current = 0;
    setPhase('loading');
    setMessage(t('addAccount.requesting'));
    setAuthUrl('');
    try {
      // region 一起发：服务端要替这张码盯着（前端被节流也不影响），而地区
      // 登记必须在落盘前完成，它那条路径读不到弹窗里的 state，只能在这里给。
      const data = await accountApi.start(realm, upstreamId, region || undefined, proxy);
      stateRef.current = data.state;
      setAuthUrl(data.authUrl);
      setPhase('waiting');
      setMessage(t('addAccount.waiting'));

      const tick = async () => {
        // 标签页在后台就不查：浏览器本来也会把定时器节流到约 1 次/分钟，
        // 与其让它零星触发，不如等用户切回来时补一次（与 useHeartbeat 同口径）。
        // 判断放在 tick 内部而不是调用处 —— 因为 visibilitychange 在**隐藏与
        // 显示时都会触发**，放外面的话「切走」那一下也会白发一次请求。
        if (document.hidden) return;
        if (pollingRef.current) return;  // 上一次还没回来，跳过本轮
        pollingRef.current = true;
        try {
          const res = await accountApi.poll(stateRef.current, realm, region || undefined,
                                            upstreamId);
          // 拿到任何一次正常响应就清零：计数要表达的是「**连续**失败」，
          // 而不是「累计失败了几次」。不清零的话，几分钟内零散抖三次
          // （每次之间都恢复正常）也会触发中断，把一次正常的扫码打断。
          failsRef.current = 0;
          if (res.status === 'success') {
            stopPoll();
            setPhase('success');
            const accountName = res.nickname || res.uid || '';
            setMessage(
              res.updated
                ? t('addAccount.successUpdated', {name: accountName})
                : t('addAccount.success', {name: accountName}),
            );
            notify.ok(
              t('addAccount.success', {name: accountName}),
              res.realm === 'global'
                ? t('addAccount.successGlobal')
                : res.updated
                  ? t('addAccount.successToken')
                  : t('addAccount.successCheckin'),
            );
            window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
            onSuccess?.();
            window.setTimeout(() => onOpenChange(false), 1600);
          } else if (res.status === 'expired') {
            // 真过了有效期（5 分钟）：重新取一张码才是对的
            stopPoll();
            setPhase('error');
            setMessage(t('addAccount.qrExpired'));
          } else if (res.status === 'invalid') {
            // 与「过期」分开报：state 不在缓存里意味着**服务端重启过**
            // （或部署成多进程），二维码本身没问题。以前两者共用一句
            // 「二维码已失效」，用户会一直重扫却怎么都不成功（issue #26）。
            stopPoll();
            setPhase('error');
            setMessage(t('addAccount.stateLost'));
          } else if (res.status === 'realm_mismatch') {
            stopPoll();
            setPhase('error');
            setMessage(t('addAccount.realmMismatch'));
          }
        } catch (e) {
          // 不再静默吞掉：轮询出错（落盘失败 / 网络抖动 / 上游超时）以前会被
          // 丢弃并继续轮询，最后必然走到「二维码已失效」——把真实原因藏了起来。
          // 现在连续失败到阈值就报出来，并保留重试入口。
          failsRef.current += 1;
          if (failsRef.current >= POLL_FAIL_LIMIT) {
            stopPoll();
            setPhase('error');
            setMessage(errText(e));
          }
        } finally {
          pollingRef.current = false;
        }
      };

      tickRef.current = tick;
      timerRef.current = window.setInterval(tick, 2000);
      // **兜底不能只挂可见性**：「切回标签页立即补一次」挂在 visibilitychange 上，
      // 但 Chrome 对**被遮挡**的窗口（授权页盖住面板就是这样）会节流定时器——
      // 实测 2 秒降到约 1 分钟一次——而 document.hidden 仍是 false、
      // visibilitychange 一次都不触发，那条兜底因此完全失效，用户切回来只能干等。
      //
      // 所以再挂在「用户确实在操作这个页面」的事件上：他回来看结果必然要点一下、
      // 敲一下或让窗口获得焦点，那一刻立即补一次。这些事件不受遮挡节流影响。
      // 检测本身已经在服务端跑了（见 server/routers/accounts.py 的说明），
      // 这里只是去读结果，所以补得越勤也只是读一次内存。
      wakeEvents().forEach(([target, event]) => target.addEventListener(event, tick));
    } catch (e) {
      setPhase('error');
      setMessage(errText(e));
    }
  }, [onOpenChange, onSuccess, stopPoll, realm, region, proxy, t, upstreamId]);

  useEffect(() => {
    if (open && routesReady) {
      start();
    } else {
      stopPoll();
    }
    return stopPoll;
    // 版本或地区变化时重新申请：扫码码是绑定端点的，旧码不能跨版本用
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, realm, region, upstreamId, proxy, routesReady]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[420px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('addAccount.title', {realm: realmName})}</DialogTitle>
          <DialogDescription>
            {realm === 'global'
              ? t('addAccount.descGlobal')
              : t('addAccount.descCn')}
          </DialogDescription>
        </DialogHeader>

        <div className="flex w-full min-w-0 flex-col items-center gap-4 px-6 pb-6">
          {routeError && <p role="alert" className="text-xs text-destructive">{routeError}</p>}
          {/* 没有配置任何线路时整个选择器都不出现：那时下拉里只有「直连」一项，
              摆出来只会让人以为能选却选不动（线路表读自上游配置的 proxies）。 */}
          {proxyRoutes.length > 0 && (
            <div className="w-full space-y-1.5">
              <div className="text-[11px] font-medium">{t('accounts.proxyLine')}</div>
              <Select value={proxy ? `route:${proxy}` : 'default'} disabled={!routesReady}
                      onValueChange={(value) => setProxy(value === 'default' ? '' : value.slice(6))}>
                <SelectTrigger className="h-9 w-full rounded-full text-xs" aria-label={t('accounts.proxyLine')}>
                <SelectValue />
              </SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">{t('accounts.proxyDirect')}</SelectItem>
                  {proxy && !proxyRoutes.includes(proxy) &&
                    <SelectItem value={`route:${proxy}`}>{proxy} ({t('accounts.proxyMissing')})</SelectItem>}
                  {proxyRoutes.map((route) => <SelectItem key={route} value={`route:${route}`}>{route}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
          )}
          {/* 国际版必须先完成地区注册，否则聊天报 14017。
              放在二维码之前：地区一变就要重新申请授权码，先选好再扫省得白扫。 */}
          {realm === 'global' && (
            <div className="w-full space-y-1.5">
              <div className="text-[11px] font-medium">{t('addAccount.regionLabel')}</div>
              <Select value={region} onValueChange={setRegion}>
                <SelectTrigger className="h-9 w-full rounded-full text-xs">
                  <SelectValue placeholder={t('addAccount.regionPlaceholder')} />
                </SelectTrigger>
                <SelectContent>
                  {INTERNATIONAL_REGIONS.map((r) => (
                    <SelectItem key={r.code} value={r.code} className="text-xs">
                      {t('addAccount.regionOption', {label: t(r.key), code: r.code})}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!region && (
                <p className="text-[10px] leading-4 text-muted-foreground">
                  {t('addAccount.regionHint')}
                </p>
              )}
            </div>
          )}

          {/* 固定尺寸，避免 loading/waiting/error 各阶段弹窗高度跳动 */}
          <div className="grid h-[212px] w-[212px] shrink-0 place-items-center overflow-hidden rounded-2xl bg-white p-3 ring-1 ring-black/5">
            {phase === 'loading' && <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />}
            {phase === 'error' && <AlertTriangle className="h-7 w-7 text-amber-500" />}
            {(phase === 'waiting' || phase === 'success') && authUrl && (
              <QRCodeSVG value={authUrl} size={188} level="M" />
            )}
          </div>

          {authUrl && (
            <div className="w-full space-y-2">
              {/* 链接本身可点开；旁边给复制与分享，便于把授权链接发给朋友 */}
              <div className="flex w-full items-center gap-1.5 rounded-full bg-muted px-3 py-1.5">
                <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground" />
                <a
                  href={authUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={authUrl}
                  className="min-w-0 flex-1 truncate text-[11px] text-blue-500 hover:underline"
                >
                  {authUrl}
                </a>
              </div>
              <div className="flex flex-wrap items-center justify-center gap-2">
                <CopyButton
                  value={authUrl}
                  size="sm"
                  showLabel
                  label={t('addAccount.copyLink')}
                  variant="outline"
                  className="rounded-full"
                />
                <ShareButton
                  title={t('addAccount.shareTitle')}
                  text={t('addAccount.shareText')}
                  url={authUrl}
                />
              </div>
            </div>
          )}

          <div
            className={
              'flex items-center gap-2 px-2 text-xs ' +
              (phase === 'success' ?
                'text-emerald-500' :
                phase === 'error' ?
                  'text-red-500' :
                  'text-muted-foreground')
            }
          >
            {phase === 'waiting' && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />}
            {phase === 'success' && <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />}
            <span className="text-center">{message}</span>
          </div>

          <div className="flex w-full gap-2">
            <Button variant="outline" className="flex-1 rounded-full" onClick={() => onOpenChange(false)}>
              {t('common.cancel')}
            </Button>
            {phase === 'error' && (
              <Button className="flex-1 rounded-full" onClick={start}>
                {t('addAccount.retry')}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
