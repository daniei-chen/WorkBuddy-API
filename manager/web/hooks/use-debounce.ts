'use client';

import {useEffect, useState} from 'react';

/**
 * 防抖钩子函数
 * @param value - 需要防抖的值
 * @param delay - 延迟时间，单位毫秒，默认为300ms
 * @returns 防抖后的值
 */
export function useDebounce<T>(value: T, delay = 300): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedValue(value);
    }, delay);

    return () => {
      clearTimeout(timer);
    };
  }, [value, delay]);

  return debouncedValue;
}
