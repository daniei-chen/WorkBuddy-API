'use client';

import {useEffect, useState} from 'react';
import {useRouter} from 'next/navigation';
import {Bot, Loader2, LogIn} from 'lucide-react';
import {errText} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {useT} from '@/lib/i18n/provider';

export default function LoginPage() {
  const {me, loading, login} = useAuth();
  const router = useRouter();
  const t = useT();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!loading && me) router.replace('/dashboard');
  }, [loading, me, router]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError('');

    // 提交值以**表单元素的实际值**为准，而不是 React state。
    //
    // 为什么必须这样：浏览器自动填充是**直接改写 DOM 的 value、不派发 `input`
    // 事件**，而受控组件的 `onChange` 依赖 `input` 事件——于是 state 始终是空串，
    // 提交出去的就是空密码。界面显示有值（来自 DOM）、服务端收到的却是空值，
    // 用户只看到「用户名或密码错误」，会以为是自己记错了密码，反复重试直到
    // 触发登录锁定（实测复现：DOM 里 13 位密码，请求体是
    // `{"username":"","password":""}`）。
    //
    // 从 DOM 读值同时覆盖三种情况：手输（state 与 DOM 一致）、自动填充（只有
    // DOM 有值）、密码管理器注入——都不依赖事件是否派发。
    const form = e.currentTarget as HTMLFormElement;
    const domUser = (form.elements.namedItem('username') as HTMLInputElement | null)?.value ?? '';
    const domPass = (form.elements.namedItem('password') as HTMLInputElement | null)?.value ?? '';
    const finalUser = (domUser || username).trim();
    const finalPass = domPass || password;

    // 读不到值：给一条**指向真正原因**的提示。落到后端只会得到
    // 「用户名或密码错误」，把「填充没被识别」误报成「密码错」，用户无从下手。
    if (!finalUser || !finalPass) {
      setError(t('login.emptySubmit'));
      return;
    }

    setBusy(true);
    try {
      await login(finalUser, finalPass);
      router.replace('/dashboard');
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bg-background relative flex min-h-svh flex-col items-center justify-center gap-6 p-6 md:p-10">
      {/* 未登录也要能换语言：看不懂当前语言的人得先能切过去 */}
      <div className="absolute right-4 top-4">
        <LanguageToggle />
      </div>
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid h-11 w-11 place-items-center rounded-2xl bg-primary text-primary-foreground">
            <Bot className="h-5 w-5" />
          </div>
          <div className="space-y-1">
            <h1 className="text-lg font-semibold tracking-[-0.01em]">WorkBuddy Manager</h1>
            <p className="text-xs text-muted-foreground">
              {t('login.subtitle')}
            </p>
          </div>
        </div>

        <form onSubmit={submit} className="rounded-[24px] bg-muted p-5">
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="username" className="text-[11px] text-muted-foreground">
                {t('login.username')}
              </Label>
              <Input
                id="username"
                name="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="admin"
                className="bg-background"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password" className="text-[11px] text-muted-foreground">
                {t('login.password')}
              </Label>
              <Input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('login.passwordPlaceholder')}
                className="bg-background"
              />
            </div>

            {error && <p className="text-xs text-red-500">{error}</p>}

            <Button type="submit" disabled={busy} className="w-full rounded-full">
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
              {t('login.submit')}
            </Button>
          </div>
        </form>

        <p className="mt-6 text-center text-[11px] text-muted-foreground">
          {t('login.legal')}
        </p>
      </div>
    </div>
  );
}
