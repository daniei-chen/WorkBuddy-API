import {clsx, type ClassValue} from 'clsx';
import {twMerge} from 'tailwind-merge';

import {intlLocale, t} from '@/lib/i18n';
import {copyText} from '@/lib/format';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * 格式化日期为可读的字符串
 * @param dateString - ISO日期字符串
 * @returns 格式化后的日期字符串
 */
export function formatDate(dateString: string): string {
  try {
    const date = new Date(dateString);
    return date.toLocaleDateString(intlLocale(), {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    });
  } catch {
    return dateString;
  }
}

/**
 * 格式化日期时间为可读的字符串
 * @param dateString - ISO日期字符串
 * @returns 格式化后的日期时间字符串
 */
export function formatDateTime(dateString: string): string {
  try {
    const date = new Date(dateString);
    return date.toLocaleString(intlLocale(), {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return dateString;
  }
}

/**
 * 格式化日期时间为精确到秒的字符串
 * @param dateString - ISO日期字符串
 * @returns 格式化后的日期时间字符串（精确到秒）
 */
export function formatDateTimeWithSeconds(dateString: string): string {
  try {
    const date = new Date(dateString);
    return date.toLocaleString(intlLocale(), {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return dateString;
  }
}

/**
 * 复制文本到剪贴板（失败时抛错）。
 *
 * **实现只有一份**：委托给 `format.copyText`。这里原先自己写了一遍同样的
 * 「Clipboard API + execCommand 回退」，而那条回退同样忽略了 execCommand 的
 * 返回值 —— 同一个 bug 存了两份（issue #57）。合并成一份，免得下次只修一处。
 *
 * @param text - 要复制的文本
 * @throws 复制未真正成功时抛出（调用方据此如实提示，而不是报「已复制」）
 */
export async function copyToClipboard(text: string): Promise<void> {
  const ok = await copyText(text);
  if (!ok) {
    throw new Error(t('common.copyFailed'));
  }
}
