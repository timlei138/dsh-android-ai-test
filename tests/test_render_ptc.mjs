/** 复现并钉住 `content.some is not a function`。
 *
 * 报错现场（测试模式 / PTC）：
 *     dsh-tools/lib/types/ptc.js:458
 *     if (!result.isError && result.content.some(block => block.type === 'image'))
 *     → TypeError: content.some is not a function
 *
 * 根因链（读 DSH 源码确认）：
 *     index.js:3422  rendered = tool.output.render(exec.arguments, value)
 *     index.js:3426  const content = snapshotProjection(tool.name, "render", rendered)
 *     index.js:3466  content: result.content          ← render 的返回值**原样**当 content
 *
 *   也就是 render 返回什么，content 就是什么。返回字符串 → 没有 .some()。
 *
 *   类型定义（dsh-tools 里的 ToolOutputDefinition）写得很清楚：
 *       render(args, value): ContentBlock[]
 *
 * 这个测试直接把 render 的输出喂给 ptc 那一行等价的表达式，
 * 保证同类问题在插件侧就被拦住。
 */
import { pathToFileURL } from "node:url";

const F = "D:/dsh/langgraph-poc/runner-tool/lib/index.js";

let pass = 0, fail = 0;
function check(name, cond, extra = "") {
  if (cond) { pass++; console.log(`  [PASS] ${name}` + (extra ? `  -> ${extra}` : "")); }
  else { fail++; console.log(`  [FAIL] ${name}` + (extra ? `  -> ${extra}` : "")); }
}

const mod = await import(pathToFileURL(F).href + "?v=ptc-" + Date.now());
let tool = null;
mod.apply({ tools: { register: (t) => { tool = t; }, defineTool: (t) => t } });
check("插件加载并注册", tool?.name === "android_case", String(tool?.name));

// 模拟一次真实 execute 的返回值
const value = {
  status: "PASS",
  engine: "model",
  report: "结论：PASS  步骤 4/4\n预期 3/3\n引擎：model",
};

console.log("\n=== 1) render 的输出必须能通过 ptc.js:458 那一行 ===");
const content = tool.output.render({ case_name: "168" }, value);
let ptcOk = false, ptcErr = "";
try {
  // ptc.js:458 的等价表达式（原样抄过来）
  ptcOk = !false && content.some((block) => block.type === "image");
} catch (e) {
  ptcErr = `${e.constructor.name}: ${e.message}`;
}
check("content.some() 可调用（不抛 TypeError）", ptcErr === "", ptcErr || "OK");
check("结果是布尔值", typeof ptcOk === "boolean", String(ptcOk));

console.log("\n=== 2) 必须是 ContentBlock[]，且内容没丢 ===");
check("Array.isArray(content)", Array.isArray(content), JSON.stringify(content));
check("每个 block 都有 type",
      Array.isArray(content) && content.every((b) => typeof b?.type === "string"));
const text = Array.isArray(content)
  ? content.filter((b) => b.type === "text").map((b) => b.text).join("\n")
  : "";
check("report 原文完整带出",
      text === value.report, JSON.stringify(text).slice(0, 80));

console.log("\n=== 3) 边界：report 为空/缺失时也不能炸 ===");
for (const v of [
  { status: "BLOCKED", engine: "", report: "" },
  { status: "PASS", engine: "model" },                    // report 缺失
  { status: "PASS", engine: "model", report: null },
]) {
  try {
    const out = tool.output.render({}, v);
    const ok = Array.isArray(out) &&
      out.every((b) => typeof b?.type === "string" && typeof b.text === "string");
    check(`report=${JSON.stringify(v.report)} 仍返回合法块`, ok,
          JSON.stringify(out));
  } catch (e) {
    check(`report=${JSON.stringify(v.report)} 仍返回合法块`, false, e.message);
  }
}

console.log("\n=== 4) 自检必须拦住「render 返回字符串」（回归）===");
// 这条是本次 bug 的本质：以前自检断言的正是字符串形态。
const { readFileSync, writeFileSync, unlinkSync } = await import("node:fs");
const orig = readFileSync(F, "utf8");
const BAK = F + ".ptc.bak";
try {
  const bad = orig.replace(
    /\r?\n {8}render\(_args, value\) \{[\s\S]*?\r?\n {8}\},/,
    "\n        render(_args, value) {\n          return value.report;\n        },");
  if (bad === orig) {
    check("能构造字符串版 render", false, "正则没匹配");
  } else {
    writeFileSync(BAK, orig);
    writeFileSync(F, bad);
    try {
      const m2 = await import(pathToFileURL(F).href + "?v=ptc-bad-" + Date.now());
      m2.apply({ tools: { register: () => {} } });
      check("字符串版 render 被自检拦住", false, "没拦住");
    } catch (e) {
      const msg = String(e?.message ?? e);
      check("字符串版 render 被自检拦住", /ContentBlock/.test(msg),
            msg.split("\n")[0]);
    } finally {
      writeFileSync(F, orig);
      try { unlinkSync(BAK); } catch {}
    }
  }
} catch (e) {
  writeFileSync(F, orig);
  try { unlinkSync(BAK); } catch {}
  check("字符串版 render 被自检拦住", false, e.message);
}

console.log(`\n${"=".repeat(52)}`);
console.log(`  ${fail ? `${fail} 项失败` : "全部通过"}（${pass + fail} 项）`);
console.log("=".repeat(52));
process.exit(fail ? 1 : 0);
