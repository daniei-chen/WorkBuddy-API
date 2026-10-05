'use client';

import {motion} from 'motion/react';
import type {ReactNode} from 'react';
import type {LucideIcon} from 'lucide-react';
import {cn} from '@/lib/utils';

/**
 * 数值语义色调。统一在此定义，避免各页面各写一套颜色，
 * 也让「好 / 注意 / 坏」在不同页面保持一致的视觉语言。
 */
export type StatTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info' | 'accent';

const TONE_VALUE: Record<StatTone, string> = {
  neutral: 'text-gray-900 dark:text-gray-100',
  success: 'text-emerald-600 dark:text-emerald-400',
  warning: 'text-amber-600 dark:text-amber-400',
  danger: 'text-red-600 dark:text-red-400',
  info: 'text-blue-600 dark:text-blue-400',
  accent: 'text-violet-600 dark:text-violet-400',
};

/** 图标底色也随语义变化，强化区分度 */
const TONE_ICON: Record<StatTone, string> = {
  neutral: 'text-gray-500',
  success: 'text-emerald-600 dark:text-emerald-400',
  warning: 'text-amber-600 dark:text-amber-400',
  danger: 'text-red-600 dark:text-red-400',
  info: 'text-blue-600 dark:text-blue-400',
  accent: 'text-violet-600 dark:text-violet-400',
};

/**
 * 统计卡片 —— 沿用 LDC StatCard 范式：
 * min-h-[88px] rounded-[20px] bg-muted，无边框、靠底色区分。
 */
export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  tone = 'neutral',
  hintTone,
  delay = 0,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  icon?: LucideIcon;
  /** 主数值的语义色调 */
  tone?: StatTone;
  /** 副文案的语义色调，默认跟随 neutral */
  hintTone?: StatTone;
  delay?: number;
}) {
  return (
    <motion.div
      initial={{opacity: 0, y: 12}}
      animate={{opacity: 1, y: 0}}
      transition={{duration: 0.35, delay}}
      className="min-h-[88px] sm:min-h-[96px] rounded-[20px] bg-muted px-3.5 py-3 sm:px-4"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="text-[11px] font-medium text-gray-500 dark:text-gray-400 truncate">
          {label}
        </div>
        {Icon && (
          <div
            className={cn(
              'h-6 w-6 shrink-0 rounded-full bg-white/70 dark:bg-white/[0.05] grid place-items-center',
              TONE_ICON[tone],
            )}
          >
            <Icon className="h-3.5 w-3.5" />
          </div>
        )}
      </div>
      <div
        className={cn(
          'mt-3 text-xl sm:text-2xl font-semibold tracking-[-0.03em] tabular-nums',
          TONE_VALUE[tone],
        )}
      >
        {value}
      </div>
      {hint && (
        <div
          className={cn(
            'mt-2 text-[11px]',
            hintTone ? TONE_VALUE[hintTone] : 'text-gray-500 dark:text-gray-400',
          )}
        >
          {hint}
        </div>
      )}
    </motion.div>
  );
}
