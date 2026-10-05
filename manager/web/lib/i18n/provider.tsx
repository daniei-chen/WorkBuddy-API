'use client';

/**
 * 语言上下文。
 *
 * 首帧固定渲染源语言（服务端预渲染时读不到 localStorage），挂载后再切到
 * 用户保存 / 浏览器语言——与 RealmProvider 同一取舍，代价是非中文用户
 * 整页加载时会闪一下中文，但避免了服务端与客户端首帧不一致的水合报错。
 * 站内跳转是客户端路由，切换后不会再闪。
 */
import {createContext, useCallback, useContext, useEffect, useMemo, useState} from 'react';

import {DEFAULT_LOCALE, LOCALES, STORAGE_KEY, detectLocale, type Locale} from './config';
import {setActiveLocale, tp, translator, type TParams} from './index';

export type TFn = (key: string, params?: TParams) => string;

interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: TFn;
  locales: readonly Locale[];
  /** 短语表翻译：参数是**中文源文**（用于设置页这类数据表文案） */
  tp: (source: string) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({children}: {children: React.ReactNode}) {
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE);

  useEffect(() => {
    setLocaleState(detectLocale());
  }, []);

  // 渲染期同步给模块级入口：lib/format.ts 这类纯函数在子组件渲染时被调用，
  // 放到 effect 里会晚一帧，出现“文案已换、日期格式还是旧语言”的错位。
  setActiveLocale(locale);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* 存不进就算了，本次会话内切换依然生效 */
    }
  }, []);

  const t = useMemo(() => translator(locale), [locale]);

  const value = useMemo<I18nContextValue>(
    () => ({locale, setLocale, t, tp, locales: LOCALES}),
    [locale, setLocale, t],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error('useI18n 必须在 I18nProvider 内使用');
  return value;
}

/** 只要翻译函数时的简写。 */
export function useT(): TFn {
  return useI18n().t;
}
