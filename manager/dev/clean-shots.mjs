/**
 * 清理图标 / 界面截图脚本产生的临时图片（开发工具）。
 *
 * 这些文件都写在 dev/ 下、且已被 .gitignore 忽略，用脚本清理可以避免在命令行里
 * 拼接通配符删除 —— 删除范围一目了然，也便于审阅。
 *
 *   node dev/clean-shots.mjs
 */
import fs from 'node:fs';
import path from 'node:path';

const DIR = import.meta.dirname;
const PREFIXES = [
  '.ab-', '.ab0-', '.ab1-', '.ab2-', '.ab3-', '.ab4-', '.ab5-',
  '.shot-', '.ui-', '.icon-', '.variants-', '.pick-', '.verify-',
  '.zoom-', '.cmp-', '.now-', '.dockcmp-', '.final-', '.dock-',
];

let removed = 0;
for (const entry of fs.readdirSync(DIR, {withFileTypes: true})) {
  if (!PREFIXES.some((p) => entry.name.startsWith(p))) continue;
  const target = path.join(DIR, entry.name);
  fs.rmSync(target, {recursive: true, force: true});
  console.log(`removed ${entry.name}`);
  removed += 1;
}
console.log(removed ? `共清理 ${removed} 项` : '没有需要清理的临时文件');
