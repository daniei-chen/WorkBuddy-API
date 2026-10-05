'use client';

import {Globe, House} from 'lucide-react';
import {motion} from 'motion/react';

import {useRealm, type Realm} from '@/lib/realm-context';
import {useT} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';
import {cn} from '@/lib/utils';

/**
 * 版本切换（国内版 / 国际版）。
 *
 * 放在页面右上角而非浮动底栏——底栏是照 linux-do/cdk 原样保留的，
 * 不往上加控件。切换影响账号/模型/测试台/任务的过滤与扫码登录的端点。
 *
 * compact 用于移动端：只显示图标，避免在窄屏占太多横向空间。
 */
export function RealmToggle({compact = false}: {compact?: boolean}) {
  const {realm, setRealm} = useRealm();
  const t = useT();
  const options: {id: Realm; label: string; title: string; icon: typeof House}[] = [
    {id: 'cn', label: t('realm.cn'), title: t('realm.cnTitle'), icon: House},
    {id: 'global', label: t('realm.global'), title: t('realm.globalTitle'), icon: Globe},
  ];

  return (
    <div
      className="inline-flex items-center gap-0.5 rounded-full border border-border/60 bg-muted/60 p-0.5"
      role="tablist"
      aria-label={t('realm.switch')}
    >
      {options.map(({id, label, title, icon: Icon}) => {
        const active = realm === id;
        return (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={active}
            title={title}
            onClick={() => setRealm(id)}
            className={cn(
              'relative flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors',
              active ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {active && (
              <motion.span
                layoutId="realm-pill"
                className="absolute inset-0 rounded-full bg-background shadow-sm"
                transition={{type: 'spring', stiffness: 400, damping: 32}}
              />
            )}
            <Icon className="relative h-3 w-3 shrink-0" />
            {!compact && <span className="relative">{label}</span>}
            {compact && <span className="sr-only">{label}</span>}
          </button>
        );
      })}
    </div>
  );
}

/** 页面内的版本提示条：说明当前版本会影响哪些内容 */
export function RealmNote({className}: {className?: string}) {
  const {realm} = useRealm();
  const t = useT();
  if (realm === 'cn') return null;
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-[16px] border border-sky-500/30 bg-sky-500/10 px-3.5 py-2.5 text-[11px] leading-5 text-sky-700 dark:text-sky-300',
        className,
      )}
    >
      <Globe className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      {/* 加粗位置由译文自己决定：各语言语序不同，拆成多个 JSX 片段必然错位 */}
      <RichText text={t('realm.note')} />
    </div>
  );
}
