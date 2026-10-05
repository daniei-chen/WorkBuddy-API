'use client';

import {useEffect, useState} from 'react';
import {Check, Copy, Loader2, Share2} from 'lucide-react';
import {copyText} from '@/lib/format';
import {notify} from '@/lib/toast';
import {Button} from '@/components/ui/button';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

/**
 * 通用「复制」按钮。
 *
 * 为什么做成组件：密钥、Base URL、授权链接、日志里的错误信息都需要复制，
 * 之前各处各写一遍——其中密钥那处漏了 `min-w-0`，长文本把按钮挤出了弹窗
 * （移动端点不到）。统一在这里处理尺寸、触控区域与反馈，避免再犯。
 *
 * 关键点：
 *  - `shrink-0`：自身永不被长文本挤掉
 *  - 默认 32×32 的触控区域，满足移动端最小点击尺寸
 *  - 复制成功短暂显示对勾，给出即时反馈（移动端看不到 toast 时也明确）
 */
export function CopyButton({
  value,
  label,
  title,
  className,
  variant = 'ghost',
  size = 'icon',
  showLabel = false,
  disabled = false,
}: {
  /** 要复制的内容；为空时禁用 */
  value: string;
  /** 无障碍标签 / 带文字时的按钮文案 */
  label?: string;
  /** 悬浮提示 */
  title?: string;
  className?: string;
  variant?: 'ghost' | 'outline' | 'secondary' | 'default';
  size?: 'icon' | 'sm';
  /** 是否在图标旁显示文字 */
  showLabel?: boolean;
  disabled?: boolean;
}) {
  const t = useT();
  const [state, setState] = useState<'idle' | 'done' | 'busy'>('idle');

  async function onCopy() {
    if (!value || state === 'busy') return;
    setState('busy');
    const ok = await copyText(value);
    if (ok) {
      setState('done');
      notify.ok(t('common.copiedToClipboard'));
      window.setTimeout(() => setState('idle'), 1500);
    } else {
      setState('idle');
      notify.err(t('common.copyFailed'), t('common.manualCopy'));
    }
  }

  const text = label || title || t('common.copy');

  return (
    <Button
      type="button"
      variant={variant}
      size={size}
      disabled={disabled || !value}
      title={text}
      aria-label={text}
      onClick={onCopy}
      // shrink-0：长文本所在的 flex 行里，按钮必须保持可见
      className={cn('shrink-0', size === 'icon' && 'h-8 w-8 rounded-md', className)}
    >
      {state === 'busy' ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
      ) : state === 'done' ? (
        <Check className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
      ) : (
        <Copy className="h-3.5 w-3.5" />
      )}
      {showLabel && <span className="ml-1.5">{state === 'done' ? t('common.copied') : text}</span>}
    </Button>
  );
}

/**
 * 「分享」按钮：仅在浏览器支持 Web Share API 时渲染（多为移动端）。
 * 用于把添加账号的授权链接直接发给朋友；不支持时按钮不出现，
 * 用户仍可用旁边的复制按钮手动发送。
 */
export function ShareButton({
  title,
  text,
  url,
  className,
  disabled = false,
}: {
  title: string;
  text?: string;
  url: string;
  className?: string;
  disabled?: boolean;
}) {
  const t = useT();
  const [supported, setSupported] = useState(false);
  // 挂载后再判断：静态导出阶段没有 navigator，直接读会报错
  useEffect(() => {
    if (typeof navigator !== 'undefined' && typeof navigator.share === 'function') {
      setSupported(true);
    }
  }, []);

  if (!supported || !url) return null;

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={cn('rounded-full', className)}
      disabled={disabled}
      onClick={async () => {
        try {
          await navigator.share({title, text, url});
        } catch {
          /* 用户取消分享：不提示 */
        }
      }}
    >
      <Share2 className="h-3.5 w-3.5" />
      {t('common.share')}
    </Button>
  );
}
