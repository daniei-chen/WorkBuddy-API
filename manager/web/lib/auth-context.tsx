'use client';

import {createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode} from 'react';
import {authApi} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import type {Me, Role} from '@/lib/types';

const ME_CACHE_KEY = 'wb-me';

/** 缓存登录态，避免每次整页加载都先空一下再填充 */
function readCachedMe(): Me | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(ME_CACHE_KEY);
    return raw ? (JSON.parse(raw) as Me) : null;
  } catch {
    return null;
  }
}

function writeCachedMe(me: Me | null): void {
  if (typeof window === 'undefined') return;
  try {
    if (me) window.sessionStorage.setItem(ME_CACHE_KEY, JSON.stringify(me));
    else window.sessionStorage.removeItem(ME_CACHE_KEY);
  } catch {
    /* 隐私模式下 sessionStorage 可能不可用，忽略 */
  }
}

interface AuthContextValue {
  me: Me | null;
  loading: boolean;
  isAdmin: boolean;
  role: Role | null;
  refresh: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({children}: {children: ReactNode}) {
  const [me, setMe] = useState<Me | null>(null);
  // 缓存只用于尽快恢复用户信息；认证状态仍由服务端校验决定。
  const [loading, setLoading] = useState(true);
  const hydrated = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const data = await authApi.me();
      setMe(data);
      writeCachedMe(data);
    } catch {
      setMe(null);
      writeCachedMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await authApi.login(username, password);
    const next = {username: res.username, role: res.role as Role};
    setMe(next);
    writeCachedMe(next);
    setLoading(false);
  }, []);

  const logout = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* 忽略登出异常 */
    }
    setMe(null);
    writeCachedMe(null);
    // 裸跳转不走 next/router，basePath 不会自动生效，必须显式拼上
    if (typeof window !== 'undefined') window.location.href = `${BASE_PATH}/login`;
  }, []);

  useEffect(() => {
    if (hydrated.current) return;
    hydrated.current = true;
    // 先用缓存恢复界面信息，等待服务器校验后再判断是否已登录，避免失效缓存导致来回跳转。
    const cached = readCachedMe();
    if (cached) {
      setMe(cached);
    }
    refresh();
  }, [refresh]);

  return (
    <AuthContext.Provider
      value={{me, loading, isAdmin: me?.role === 'admin', role: me?.role ?? null, refresh, login, logout}}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return ctx;
}
