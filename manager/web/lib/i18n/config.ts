/**
 * 语言清单与浏览器语言识别。
 *
 * 为什么不用路由前缀（`/en/dashboard`）：前端是 `next build` 静态导出、由
 * FastAPI 直接托管文件，加语言段要改所有跳转与后端静态路由；而管理端的
 * 语言偏好本来就是单个使用者的本地设置，客户端切换 + localStorage 记忆
 * 更贴合，也不影响既有部署。
 */
export const LOCALES = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko'] as const;

export type Locale = (typeof LOCALES)[number];

/** 简体中文是源语言：其它语言缺键时回退到它。 */
export const DEFAULT_LOCALE: Locale = 'zh-CN';

export const STORAGE_KEY = 'workbuddy-manager:locale';

/**
 * 语言自称（native name）：切换器里一律显示母语写法，
 * 让看不懂当前界面语言的用户也能认出自己的语言。
 */
export const LOCALE_LABELS: Record<Locale, string> = {
  'zh-CN': '简体中文',
  'zh-TW': '繁體中文',
  en: 'English',
  ja: '日本語',
  ko: '한국어',
};

export function isLocale(value: string): value is Locale {
  return (LOCALES as readonly string[]).includes(value);
}

/**
 * BCP-47 语言标签 → 支持的语种。
 *
 * 中文要特别处理：zh-Hant / zh-TW / zh-HK / zh-MO 都是繁体，
 * 其余 zh-*（含 zh-Hans、zh-SG）按简体处理。
 */
export function matchLocale(tag: string): Locale | null {
  const t = tag.toLowerCase();
  if (t.startsWith('zh')) {
    return /hant|tw|hk|mo/.test(t) ? 'zh-TW' : 'zh-CN';
  }
  if (t.startsWith('en')) return 'en';
  if (t.startsWith('ja')) return 'ja';
  if (t.startsWith('ko')) return 'ko';
  return null;
}

/**
 * 决定首屏用哪种语言：已保存的显式选择 > 浏览器语言 > 简体中文。
 * 只在客户端调用（服务端预渲染时没有 localStorage / navigator）。
 */
export function detectLocale(): Locale {
  if (typeof window === 'undefined') return DEFAULT_LOCALE;
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved && isLocale(saved)) return saved;
  } catch {
    /* 隐私模式下 localStorage 可能抛错：忽略，走浏览器语言 */
  }
  const tags = navigator.languages?.length ? navigator.languages : [navigator.language];
  for (const tag of tags) {
    if (!tag) continue;
    const matched = matchLocale(tag);
    if (matched) return matched;
  }
  return DEFAULT_LOCALE;
}
