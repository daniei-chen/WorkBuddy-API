/**
 * i18n 扫描器（开发工具，不参与构建）。
 *
 * 用 TypeScript 编译器 API 解析 web/ 下的 .ts/.tsx，收集**用户可见的中文字符串**：
 *   - 字符串字面量（'...' / "..." / `...` 无插值模板）
 *   - JSX 文本节点
 * 注释与 import 路径会被自动排除（注释不是节点，导入路径不含中文）。
 *
 * 用途：估算多语言改造的工作量、检查是否有漏提取的硬编码文案。
 *   node dev/i18n-scan.mjs            汇总统计
 *   node dev/i18n-scan.mjs --list     列出所有唯一文案及出现次数
 *   node dev/i18n-scan.mjs --file <p> 只列某个文件
 */
import fs from 'node:fs';
import path from 'node:path';
import ts from '../web/node_modules/typescript/lib/typescript.js';

const ROOT = path.resolve(import.meta.dirname, '..');
const WEB = path.join(ROOT, 'web');
const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;

function walkDir(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    if (entry.name === 'node_modules' || entry.name === '.next' || entry.name === 'out') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walkDir(full));
    else if (/\.(ts|tsx)$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** 从 ast 中提取所有「可能是文案」的中文片段 */
function collect(file) {
  const text = fs.readFileSync(file, 'utf8');
  // .ts 用 TS 语法解析：TSX 会把 `get<T>(...)` 这类泛型调用误当成 JSX，
  // 结果把注释也卷进字符串节点，扫描出来的「文案」就失真了。
  const kind = file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const found = [];

  const visit = (node) => {
    // 纯字符串字面量与无插值模板
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      const value = node.text;
      // 排除 import/require 的模块路径与 URL
      const parent = node.parent;
      const isModuleSpec =
        (ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent)) &&
        parent.moduleSpecifier === node;
      if (!isModuleSpec && CJK.test(value)) found.push(value);
    } else if (ts.isJsxText(node)) {
      const value = node.text.replace(/\s+/g, ' ').trim();
      if (value && CJK.test(value)) found.push(value);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return found.map((v) => ({value: v, line: lineOf(text, v)}));
}

function lineOf(text, value) {
  const idx = text.indexOf(value);
  return idx < 0 ? 0 : text.slice(0, idx).split('\n').length;
}

const args = process.argv.slice(2);
const onlyFile = args.includes('--file') ? args[args.indexOf('--file') + 1] : null;
const files = walkDir(WEB).filter((f) => (onlyFile ? f.endsWith(onlyFile) : true));

const perFile = new Map();
const unique = new Map();
for (const file of files) {
  const items = collect(file);
  if (!items.length) continue;
  perFile.set(path.relative(ROOT, file), items.length);
  for (const {value} of items) unique.set(value, (unique.get(value) ?? 0) + 1);
}

if (args.includes('--list')) {
  const sorted = [...unique.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  for (const [value, count] of sorted) {
    console.log(`${String(count).padStart(3)}  ${JSON.stringify(value)}`);
  }
} else {
  const total = [...perFile.values()].reduce((a, b) => a + b, 0);
  const rows = [...perFile.entries()].sort((a, b) => b[1] - a[1]);
  for (const [file, count] of rows) console.log(`${String(count).padStart(5)}  ${file}`);
  console.log(`\n出现次数合计 ${total}；唯一文案 ${unique.size}；涉及文件 ${perFile.size}`);
}
