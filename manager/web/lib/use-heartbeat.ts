'use client';

import {useEffect, useRef} from 'react';

/**
 * 心跳刷新：页面停留期间按固定间隔重新拉取数据。
 *
 * 有了它就不需要页面上再放一个「刷新」按钮——浏览器自带的刷新也能用。
 * 两个细节：
 *  - 标签页不可见时跳过刷新（浏览器本来也会把定时器节流到约 1 次/分钟，
 *    与其让它零星触发，不如明确跳过）；
 *  - 重新切回该标签页时立即刷新一次，避免看到切走之前的旧数据。
 */
export function useHeartbeat(fn: () => void, ms: number) {
  // 用 ref 保存最新的回调，这样 fn 每次渲染变化都不会重建定时器
  const ref = useRef(fn);
  ref.current = fn;

  useEffect(() => {
    const tick = () => {
      if (!document.hidden) ref.current();
    };
    const timer = window.setInterval(tick, ms);
    document.addEventListener('visibilitychange', tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [ms]);
}
