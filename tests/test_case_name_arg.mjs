/**
 * 回归测试：`android_case(case_name="168")` 必须能通过参数校验。
 *
 * 修的 bug：`parameters.asserts` 上多写了一个 `required: true`。
 *   defineTool 会把参数对象里每个 `required: true` 收集成**根级** JSON Schema 的
 *   `required: [...]`（dsh-tools lib/types/schema.js L81-82），
 *   于是 harness 在进 execute **之前**就要求 asserts 必须存在。
 *
 *   而 execute 第一行是 `args.case_name ? {只传 case_name, ...} : {...}` ——
 *   走 YAML 的分支本来就不该填 asserts。于是：
 *     · `android_case(case_name="168")` 被 harness 拦下
 *     · 报 "missing required property case_id/title/package/steps/asserts"
 *     · **execute 一次都没跑**，插件里的 case_name 分支成了死代码
 *   这与 case_name 自己的说明（"填了这个就不用再填 steps/asserts"）直接矛盾。
 *
 * 为什么值得单独测：这个 bug 的表现是"报错说缺一堆字段"，看起来**像调用方传错了**，
 * 而不像插件写错了 —— 很容易被误诊成"子 Agent 没按格式调用"。实测就这么误导过一次。
 */
import { pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const TOOL = pathToFileURL("D:/dsh/langgraph-poc/runner-tool/lib/index.js").href;

const PASS = [], FAIL = [];
const check = (n, ok, d = "") => {
  (ok ? PASS : FAIL).push(n);
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${n}${d ? `  -> ${d}` : ""}`);
};

const mod = await import(TOOL);

let registered = null;
mod.apply({ tools: { register(def) { registered = def; return () => {}; } } });

console.log("=== 1. 根级 required 里不能有 asserts/steps ===");
// 直接问 dsh-tools 要**归一化后**的 JSON Schema —— 不能自己猜，
// 因为 required 是 defineTool 从每个属性上的 required:true 收集出来的。
const req = createRequire(TOOL);
let normalized = null;
try {
  const dshTools = req("@deepseek-ai/dsh-tools");
  const spec = registered.parameters ?? {};
  normalized =
    dshTools.parameterSchemaSpecToJsonSchema?.(spec) ??
    dshTools.validateArgs?.(spec, {}) ??
    null;
} catch (e) {
  console.log(`      (取不到 dsh-tools：${e.message}，改用本地复刻规则)`);
}

// 本地复刻 dsh-tools 的收集规则：属性上 required===true → 进根级 required
const collectedRequired = Object.entries(registered.parameters ?? {})
  .filter(([, v]) => v?.required === true)
  .map(([k]) => k);

console.log(`      收集到的根级 required: ${JSON.stringify(collectedRequired)}`);
check("asserts 不在根级 required", !collectedRequired.includes("asserts"),
  collectedRequired.includes("asserts") ? "仍在 —— case_name 调用会被拦" : "已清除");
check("steps 不在根级 required", !collectedRequired.includes("steps"));
check("case_id/title/package 不在根级 required",
  !["case_id", "title", "package"].some((k) => collectedRequired.includes(k)));

if (normalized && Array.isArray(normalized.required)) {
  console.log(`      dsh-tools 归一化后 required: ${JSON.stringify(normalized.required)}`);
  check("归一化 schema 里也没有 asserts",
    !normalized.required.includes("asserts"));
}

console.log("\n=== 2. {case_name} 单参数必须能过校验 ===");
check("工具注册成功", registered !== null);
check("工具名是 android_case", registered?.name === "android_case", registered?.name);

// 复刻 validateArgs：用 dsh-tools 的校验器跑一次真实入参
try {
  const dshTools = req("@deepseek-ai/dsh-tools");
  const violations = dshTools.validateArgs(registered.parameters, {
    case_name: "168",
  });
  const ok = Array.isArray(violations) && violations.length === 0;
  check("validateArgs({case_name}) 无违规", ok,
    ok ? "干净" : JSON.stringify(violations));
} catch (e) {
  // 拿不到校验器就退化成"根级 required 检查"（上面已测）
  const ok = !collectedRequired.includes("asserts");
  check("validateArgs 不可用，退化为 required 检查", ok, e.message);
}

console.log("\n=== 3. RUNNER_PY 必须指向**编排** venv（不是 assets/.venv）===");
// ⚠️ 这条最容易搞反，所以专门钉住：
//   run_case_json.py --import--> subagent --> child_agent --> langgraph
//   langgraph 只在 langgraph-poc/.venv 里；assets/.venv 里是 adbutils（设备操作）。
//   把这里改成 assets/.venv 会 ImportError: No module named 'langgraph'。
//   实测两个 venv 的依赖清单确认过 —— 我自己就先改错了一次。
const src = (await import("node:fs")).readFileSync(
  "D:/dsh/langgraph-poc/runner-tool/lib/index.js", "utf8");
const at = src.indexOf("const RUNNER_PY");
const runnerPyBlock = src.slice(at, at + 400);
check("RUNNER_PY 指向 RUNNER_DIR/.venv（编排 venv，有 langgraph）",
  /join\(RUNNER_DIR,\s*"\.venv"/.test(runnerPyBlock),
  "run_case_json.py 需要 langgraph，它只在编排 venv 里");
check("RUNNER_PY 没被改成 assets/.venv",
  !/assets",\s*"\.venv/.test(runnerPyBlock),
  "assets/.venv 没有 langgraph，改过去必 ImportError");

console.log("\n" + "=".repeat(50));
console.log(FAIL.length === 0 ? `  全部通过（${PASS.length} 项）`
  : `  ${FAIL.length} 项失败: ${FAIL.join(", ")}`);
console.log("=".repeat(50));
process.exit(FAIL.length === 0 ? 0 : 1);
