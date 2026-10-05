'use client';

import {createContext, useCallback, useContext, useEffect, useMemo, useState} from 'react';

import {t as translate} from './i18n';
import {useI18n} from './i18n/provider';

/**
 * 版本（realm）切换。
 *
 * 上游是**单实例同时支持国内版与国际版**（共用账号池，按账号 realm 或模型名
 * 前缀路由），所以这里不做两套部署，只是切换「当前在看的/要操作的是哪一套」：
 *
 *   - 账号、模型、测试台、任务记录按版本过滤
 *   - 添加账号跟随：切到国际版时扫码走国际版端点 + 地区注册
 *   - 请求日志与用量是全局记录，不按版本过滤（页面另有标注）
 *
 * 默认国内版：与上游「裸名默认 CN」的口径一致，也保证既有用户升级后
 * 看到的还是原来那套内容。
 */
export type Realm = 'cn' | 'global';

const STORAGE_KEY = 'wb.realm';

interface RealmCtx {
  realm: Realm;
  setRealm: (r: Realm) => void;
  /** 国际版标签，用于界面文案 */
  label: string;
}

const Ctx = createContext<RealmCtx | null>(null);

export function RealmProvider({children}: {children: React.ReactNode}) {
  const [realm, setRealmState] = useState<Realm>('cn');
  const {t} = useI18n();

  // 首屏后读 localStorage：直接读会让服务端渲染与客户端不一致
  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved === 'global' || saved === 'cn') setRealmState(saved);
    } catch {
      /* 隐私模式下 localStorage 可能抛错，忽略即可（用默认值） */
    }
  }, []);

  const setRealm = useCallback((r: Realm) => {
    setRealmState(r);
    try {
      window.localStorage.setItem(STORAGE_KEY, r);
    } catch {
      /* 同上：存不进就算了，不影响本次会话内的切换 */
    }
  }, []);

  const value = useMemo<RealmCtx>(
    () => ({realm, setRealm, label: realm === 'global' ? t('realm.global') : t('realm.cn')}),
    [realm, setRealm, t],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRealm(): RealmCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error('useRealm 必须在 RealmProvider 内使用');
  return v;
}

/**
 * realm 的显示名（给不带 hook 的地方用，如表格标签）。
 * 读模块级当前语言（由 I18nProvider 同步）；调用它的组件在切换语言时
 * 会随父级重渲染，因此这里不必是 Hook。
 */
export function realmLabel(r: string | null | undefined): string {
  return r === 'global' ? translate('realm.global') : translate('realm.cn');
}
