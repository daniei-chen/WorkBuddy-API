'use client';

import {useEffect, useState} from 'react';

/**
 * 共享时钟，供倒计时类展示使用。
 *
 * 为什么共享而不是每个组件各起一个 setInterval：积分倒计时会同时出现在几十上百
 * 行账号上，各起一个就是上百个定时器。更糟的是刷新频率：只有最后一分钟需要秒级
 * 精度，而「30 天后到期」这种文案一天才变一次 —— 让它每秒重渲染一次纯属浪费，
 * 账号多时是持续可见的开销。所以按**间隔分桶**共享：同一间隔的订阅者共用一个
 * 定时器，调用方按自己需要的精度选间隔。
 *
 * 标签页不可见时不推进（与 useHeartbeat 同口径）：用户看不到，算了也白算。
 * 切回来时立即对齐一次真实时间 —— 浏览器会把后台定时器节流到约 1 次/分钟，
 * 不补这一下，用户切回来会看到最多一分钟前的旧倒计时。
 */
type Listener = (now: number) => void;

interface Bucket {
  listeners: Set<Listener>;
  timer: number | null;
  tick: (() => void) | null;
}

/** 间隔（毫秒）→ 该间隔的订阅者集合与它们的共享定时器 */
const buckets = new Map<number, Bucket>();

function bucketOf(intervalMs: number): Bucket {
  let b = buckets.get(intervalMs);
  if (!b) {
    b = {listeners: new Set(), timer: null, tick: null};
    buckets.set(intervalMs, b);
  }
  return b;
}

function subscribe(intervalMs: number, fn: Listener): () => void {
  const b = bucketOf(intervalMs);
  b.listeners.add(fn);
  fn(Date.now()); // 订阅即刻给一次当前时间，避免首帧显示上一轮的旧值
  if (b.timer === null) {
    // tick 存在桶上而不是订阅闭包里：桶空了要能移除**当初注册的**那个监听器，
    // 否则每次「最后一个订阅者离开」都会漏一个 visibilitychange 回调。
    b.tick = () => {
      if (document.hidden) return;
      const now = Date.now();
      b.listeners.forEach((l) => l(now));
    };
    b.timer = window.setInterval(b.tick, intervalMs);
    document.addEventListener('visibilitychange', b.tick);
  }
  return () => {
    b.listeners.delete(fn);
    if (!b.listeners.size && b.timer !== null) {
      window.clearInterval(b.timer);
      if (b.tick) document.removeEventListener('visibilitychange', b.tick);
      b.timer = null;
      b.tick = null;
      buckets.delete(intervalMs);
    }
  };
}

/**
 * 当前时间（毫秒），按 `intervalMs` 更新一次。
 *
 * 调用方按**文案变化的频率**选间隔：秒级文案用 1000，分钟级文案用 60000。
 * 精度取得比需要的高，就是让整页跟着做无用的重渲染。
 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => subscribe(intervalMs, setNow), [intervalMs]);
  return now;
}
