/**
 * preset 校验器：用 DSH 自己的 fileComposition() 判断一个 preset 能不能挂载。
 *
 * 为什么需要它：手动开 web 会话才发现报错太慢，而且报错信息被截断
 * （实测 "missing required value (at allowParallelInProgress)" 这种要靠人猜）。
 * 这个脚本在挂载前就把问题暴露出来。
 *
 * 注意：fileComposition 会求值 !!js 表达式，所以普通 js-yaml 解析不了
 * （会报 unknown tag tag:yaml.org,2002:js），必须走这个 API。
 *
 * 用法（必须在 dsh-agent-presets 包目录下跑，或用 DSH_AGENT_PRESETS_DIR 指定）：
 *   node validate_preset.mjs <preset目录或 agent.cordis.yml 路径> [...]
 */
import { fileURLToPath, pathToFileURL } from 'node:url';
import { homedir } from 'node:os';
import { join, basename, dirname } from 'node:path';
import { existsSync, statSync } from 'node:fs';

// 本脚本可能放在别处跑，所以内部 import 一律用绝对路径解析到
// dsh-agent-presets 包 —— 否则 ERR_MODULE_NOT_FOUND。
const PRESETS_PKG = process.env.DSH_PRESETS_PKG ||
  'C:/Users/leichao4/AppData/Local/Volta/tools/image/packages/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-agent-presets';

const { fileComposition } = await import(
  pathToFileURL(join(PRESETS_PKG, 'lib/types/composition-inventory.js')).href);
const { COMPOSITION_FILE, USER_PRESET_DIR } = await import(
  pathToFileURL(join(PRESETS_PKG, 'lib/types/discovery.js')).href);

/** loader 的表达式求值器：!!js 后面是 JS 源码。 */
const evaluateExpression = (source) => Function(`"use strict";return (${source})`)();

const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh');

/** 把参数规范成 agent.cordis.yml 的路径。 */
function resolveComposition(arg) {
  if (statSync(arg).isDirectory()) return join(arg, COMPOSITION_FILE);
  return arg;
}

const args = process.argv.slice(2);
const targets = args.length
  ? args.map(resolveComposition)
  : [join(dshHome, USER_PRESET_DIR, 'probe', COMPOSITION_FILE)];

let failures = 0;
for (const target of targets) {
  const label = basename(target) === COMPOSITION_FILE
    ? basename(join(target, '..'))
    : basename(target);
  console.log(`=== ${label} ===`);
  console.log(`   ${target}`);

  if (!existsSync(target)) {
    console.log('   ❌ 文件不存在\n');
    failures++;
    continue;
  }

  try {
    const { rows } = await fileComposition(target, evaluateExpression);
    console.log(`   共 ${rows.length} 行`);
    for (const row of rows) {
      const mark = row.enabled ? '✔' : '✘';
      const cond = row.condition ? `  [cond: ${row.condition}]` : '';
      console.log(`     ${mark} ${row.entryId} -> ${row.moduleName}${cond}`);
    }
    const on = rows.filter((r) => r.enabled).length;
    console.log(`   ✅ 可加载（${on}/${rows.length} 行启用）\n`);
  } catch (e) {
    failures++;
    console.log(`   ❌ 加载失败: ${e.message}\n`);
  }
}

console.log(failures === 0 ? '全部通过' : `${failures} 个失败`);
process.exit(failures === 0 ? 0 : 1);
