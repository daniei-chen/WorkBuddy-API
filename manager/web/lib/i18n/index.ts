/**
 * i18n 内核：字典解析 + 插值 + 复数 + 非 Hook 取值入口。
 *
 * 字典用 JSON 而不是 TS：Python 侧的守卫测试（server/tests/test_web_i18n.py）
 * 能直接读取 JSON 校验各语言键集与占位符是否对齐，翻译文件也不该混入代码。
 *
 * 组件里用 useT()（components 侧），非组件环境（lib/format.ts 这类纯函数）
 * 用这里的 t() —— 它读模块级的“当前语言”，由 I18nProvider 在渲染期同步。
 */
import {DEFAULT_LOCALE, type Locale} from './config';
import zhCN from './locales/zh-CN.json';
import zhTW from './locales/zh-TW.json';
import en from './locales/en.json';
import ja from './locales/ja.json';
import ko from './locales/ko.json';

/**
 * 短语表（gettext 风格）：键是**中文源文**，值是译文。
 *
 * 用于「数据表」类文案——例如设置页每个配置项的 label / desc / caution：
 * 这类文本与上游 config.json 一一对应、条目上百，且天然成表；用中文原文当键
 * 可以省掉一层「键名 ↔ 文案」的映射维护，新加上游字段时照抄原文即可。
 * 界面文案（按钮、标题、提示）仍走常规键名，两者分工明确。
 */
type PhraseTable = Record<string, string>;

/** 复数形式：键名即 Intl.PluralRules 的分类，other 必备（兜底）。 */
export interface PluralForms {
  zero?: string;
  one?: string;
  two?: string;
  few?: string;
  many?: string;
  other: string;
}

export type MessageValue = string | PluralForms;

export interface Dict {
  [key: string]: MessageValue | Dict;
}

const DICTS: Record<Locale, Dict> = {
  'zh-CN': zhCN as Dict,
  'zh-TW': zhTW as Dict,
  en: en as Dict,
  ja: ja as Dict,
  ko: ko as Dict,
};

export type TParams = Record<string, string | number>;

let activeLocale: Locale = DEFAULT_LOCALE;

/** 供 I18nProvider 在渲染期调用，让非 Hook 的 t() 与界面语言保持一致。 */
export function setActiveLocale(locale: Locale): void {
  activeLocale = locale;
}

export function getActiveLocale(): Locale {
  return activeLocale;
}

/** 按 a.b.c 逐层取字典节点；取不到返回 undefined。 */
function lookup(dict: Dict, key: string): MessageValue | undefined {
  const parts = key.split('.');
  let node: MessageValue | Dict | undefined = dict;
  for (const part of parts) {
    if (typeof node !== 'object' || node === null) return undefined;
    node = (node as Dict)[part];
  }
  return node as MessageValue | undefined;
}

function pluralOf(forms: PluralForms, locale: Locale, count: number | undefined): string {
  if (count === undefined) return forms.other;
  let rule: Intl.LDMLPluralRule = 'other';
  try {
    rule = new Intl.PluralRules(locale).select(count);
  } catch {
    /* 老环境没有该语种的复数规则：退回 other */
  }
  return forms[rule] ?? forms.other;
}

/**
 * {name} 占位替换。参数缺失时保留原样（界面上会直接看到花括号占位符，
 * 比默默渲染成空串更容易发现漏传参）。
 */
function interpolate(text: string, params?: TParams): string {
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (raw, name: string) => {
    const value = params[name];
    return value === undefined ? raw : String(value);
  });
}

/**
 * 取译文。查找顺序：当前语言 → 源语言（简体中文）→ 键名本身。
 * 漏翻时界面显示的是键名（如 accounts.title），一眼能看出问题所在。
 */
export function translate(locale: Locale, key: string, params?: TParams): string {
  const raw = lookup(DICTS[locale], key) ?? lookup(DICTS[DEFAULT_LOCALE], key);
  if (raw === undefined) return key;
  const text = typeof raw === 'string' ? raw : pluralOf(raw, locale, numParam(params));
  return interpolate(text, params);
}

function numParam(params?: TParams): number | undefined {
  const value = params?.count;
  return typeof value === 'number' ? value : undefined;
}

/** 绑定某种语言的翻译函数（I18nProvider 用它构造 t）。 */
export function translator(locale: Locale) {
  return (key: string, params?: TParams) => translate(locale, key, params);
}

/** 非组件环境用的翻译函数：跟随界面当前语言。 */
export function t(key: string, params?: TParams): string {
  return translate(activeLocale, key, params);
}

/** 查短语表；当前语言没有该条时原样返回中文源文（不显示键、也不留空）。 */
export function translatePhrase(locale: Locale, source: string): string {
  const table = (DICTS[locale] as {phrases?: PhraseTable}).phrases;
  return table?.[source] ?? source;
}

/** 组件侧：把中文源文翻译成当前语言。 */
export function tp(source: string): string {
  return translatePhrase(activeLocale, source);
}

/** 当前语言的 BCP-47 标签，给 Intl 系列 API 用。 */
export function intlLocale(): string {
  return activeLocale;
}

/** 是否按“无空格分词”排版（中文/日文）：决定按字还是按词拆分动画等。 */
export function isCjkLocale(locale: Locale = activeLocale): boolean {
  return locale.startsWith('zh') || locale === 'ja';
}
