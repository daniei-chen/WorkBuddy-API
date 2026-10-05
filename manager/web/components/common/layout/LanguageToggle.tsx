'use client';

import {Languages} from 'lucide-react';

import {LOCALE_LABELS, type Locale} from '@/lib/i18n/config';
import {useI18n} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/**
 * 语言切换。
 *
 * 放在版本切换旁边（页面右上角）：与 RealmToggle 一样不在浮动底栏上加控件，
 * 底栏保持 LDC 原样。选项用各语言的母语写法，选完即写入 localStorage。
 */
export function LanguageToggle({className}: {className?: string}) {
  const {locale, setLocale, t, locales} = useI18n();

  return (
    <Select value={locale} onValueChange={(value) => setLocale(value as Locale)}>
      <SelectTrigger
        aria-label={t('language.switch')}
        title={t('language.switch')}
        className={cn(
          'h-6 w-auto gap-1.5 rounded-full border-border/60 bg-muted/60 px-2.5 text-[11px] font-medium',
          className,
        )}
      >
        <Languages className="h-3 w-3 shrink-0 opacity-70" />
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {locales.map((id) => (
          <SelectItem key={id} value={id} className="text-xs">
            {LOCALE_LABELS[id]}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
