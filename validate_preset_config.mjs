/**
 * 校验 preset 里每一行的 **config schema**，而不只是"模块能不能解析"。
 *
 * 踩过的坑：validate_preset.mjs 只检查 name 能不能 import，
 * 结果 persona 行缺必填的 prefix 也报"可加载"，
 * 用户一切换就炸：invalid config: $.prefix missing required value。
 *
 * 这个脚本把每行的 Config schema 也跑一遍。
 */
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
import path from "node:path";

// DSH 自带包在 dsh/node_modules/@deepseek-ai/ 下面（多一层），不是 dsh/ 同级
const DSH = "C:/Users/leichao4/AppData/Local/Volta/tools/image/packages/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai";
const PROF = "C:/Users/leichao4/.dsh/profiles/web/node_modules";

const PRESET = process.argv[2]
  || "C:/Users/leichao4/.dsh/.agent-presets/android-runner";
const YML = path.join(PRESET, "agent.cordis.yml");

/** 像真 YAML 那样把标量转成合适的类型。
 *  不然 `maxDepth: 0` 会留成字符串 "0"，
 *  schema 期望 number | "provider-managed" 就会误报不合法。 */
function coerce(v) {
  const s = v.replace(/\s+#.*$/, "").trim();
  if (s === "") return undefined;
  if (/^'.*'$/.test(s) || /^".*"$/.test(s)) return s.slice(1, -1);  // 显式引号 = 字符串
  if (/^-?\d+$/.test(s)) return Number(s);
  if (s === "true") return true;
  if (s === "false") return false;
  if (s === "null" || s === "~") return null;
  return s;
}

/** 极简 YAML 解析：只认这个文件用到的子集。
 *  踩过的坑：config 一旦开始，后续行的 indent 2 键会被误吞进 config，
 *  导致下面几行的 name 解析不到。必须按缩进严格区分。 */
function parseYml(text) {
  const rows = [];
  let cur = null;
  let configIndent = null;   // config 下子键的缩进；null 表示不在 config 里

  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/\s+#.*$/, "");
    if (!line.trim()) continue;
    const indent = line.match(/^\s*/)[0].length;
    const t = line.trim();

    // 新的 list item
    if (indent === 0 && t.startsWith("- ")) {
      if (cur) rows.push(cur);
      cur = { config: {} };
      configIndent = null;
      const m = t.slice(2).match(/^([\w-]+):\s*(.*)$/);
      if (m) cur[m[1]] = m[2].replace(/^['"]|['"]$/g, "");
      continue;
    }
    if (!cur) continue;

    // config: 开始
    if (/^config:\s*$/.test(t)) { configIndent = indent; continue; }

    const m = t.match(/^([\w-]+):\s*(.*)$/);
    if (!m) continue;
    const [, k, v] = m;
    const val = coerce(v);

    // 缩进比 config 深 → config 的子键；否则是行级键（name / id）
    if (configIndent !== null && indent > configIndent) {
      cur.config[k] = v === "" ? undefined : val;
    } else {
      configIndent = null;
      cur[k] = val;
    }
  }
  if (cur) rows.push(cur);
  return rows.filter((r) => r.id);
}

const rows = parseYml(readFileSync(YML, "utf8"));
console.log(`=== ${path.basename(PRESET)} ===`);
console.log(`  ${YML}`);
console.log(`  共 ${rows.length} 行\n`);

let bad = 0;
for (const r of rows) {
  const nm = r.name;
  if (!nm) { console.log(`  ✖ ${r.id} 没有 name`); bad++; continue; }

  let mod = null; let lastErr = '';
  const bare = nm.replace(/^@deepseek-ai\//, "");
  for (const base of [PROF, DSH]) {
    // profile 的 node_modules 里既有 scoped 目录也有平铺的包名，两种都试
    for (const spec of [`@deepseek-ai/${bare}`, bare]) {
      try {
        mod = await import(
          pathToFileURL(path.join(base, spec, "lib/index.js")).href);
        break;
      } catch (e) { lastErr = `${base.includes("profiles") ? "PROF" : "DSH"}/${spec}: ${String(e.message || e).split("\n")[0]}`; }
    }
    if (mod) break;
  }
  if (!mod) { console.log(`  ✖ ${r.id} -> ${nm}  （解析不到）`); console.log(`        ${lastErr}`); bad++; continue; }

  const Cfg = mod.Config;
  if (!Cfg) { console.log(`  ✔ ${r.id} -> ${nm}  （无 Config schema，跳过）`); continue; }

  const conf = r.config && Object.keys(r.config).length ? r.config : undefined;
  try {
    Cfg(conf);   // schemastery 的 schema 可直接调用做校验
    console.log(`  ✔ ${r.id} -> ${nm}  config OK`);
  } catch (e) {
    const msg = String(e.message || e).split("\n")[0];
    console.log(`  ✖ ${r.id} -> ${nm}  config 不合法：${msg}`);
    bad++;
  }
}

console.log(`\n${bad ? `❌ ${bad} 行有问题` : "✅ 全部通过（含 config 校验）"}`);
process.exit(bad ? 1 : 0);
