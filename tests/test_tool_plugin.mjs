/**
 * 冒烟测试：我们的 android_case 工具插件能不能被 DSH 加载。
 *
 * 为什么要单独测：
 *   插件在 preset 挂载时才 import，写错了要到"开新会话"才报错，
 *   而且报错信息常被截断（之前踩过 "missing required value" 要靠猜）。
 *   这里直接把 index.js import 进来，检查：
 *     · 三个必需导出在不在（name / inject / apply）
 *     · apply 能不能在假 ctx 上跑通（defineTool 的参数合不合法）
 *     · 注册出来的工具名和 schema 对不对
 */
import { pathToFileURL } from "node:url";

const TOOL = pathToFileURL("D:/dsh/langgraph-poc/runner-tool/lib/index.js").href;

const PASS = [], FAIL = [];
const check = (n, ok, d = "") => {
  (ok ? PASS : FAIL).push(n);
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${n}${d ? `  -> ${d}` : ""}`);
};

const mod = await import(TOOL);

console.log("=== 1. 插件导出契约 ===");
check("有 name 导出", typeof mod.name === "string", mod.name);
check("有 inject 导出", Array.isArray(mod.inject), JSON.stringify(mod.inject));
check("有 apply 导出", typeof mod.apply === "function");
check("inject 声明了 tools", mod.inject?.includes("tools"));

console.log("\n=== 2. apply 能注册工具（defineTool 参数校验）===");
let registered = null;
const fakeCtx = {
  tools: {
    register(def) {
      registered = def;
      return () => {};
    },
  },
};

try {
  mod.apply(fakeCtx);
  check("apply 跑通没抛错", true);
} catch (e) {
  check("apply 跑通没抛错", false, `${e.constructor.name}: ${e.message}`);
}

check("注册了一个工具", registered !== null);

if (registered) {
  console.log("\n=== 3. 工具契约 ===");
  check("工具名是 android_case", registered.name === "android_case",
    registered.name);
  check("有描述", typeof registered.description === "string"
    && registered.description.length > 50);

  // parameters 是**归一化后的 JSON Schema**（不是原始 spec）：
  //   {type:"object", properties:{...}, required:[...]}
  const params = registered.parameters ?? {};
  const props = params.properties ?? params;
  console.log("     入参:", Object.keys(props).join(", "));
  console.log("     必填:", JSON.stringify(params.required ?? []));

  for (const need of ["case_id", "title", "package", "steps", "asserts"]) {
    check(`入参含 ${need}`, need in props);
  }
  check("支持 preconditions（否则 pm clear 不会跑）",
    "preconditions" in props);

  // 子 Agent 不该能传 shell 命令 / 文件路径 —— 检查没有这类后门参数
  const danger = ["command", "cmd", "shell", "script", "path", "file",
                  "cwd", "code", "exec"];
  const leaked = danger.filter((d) => d in props);
  check("没有 shell/文件类后门参数", leaked.length === 0,
    leaked.length ? `泄漏: ${leaked}` : "干净");

  check("输出是结构化的", !!registered.output?.schema,
    JSON.stringify(Object.keys(registered.output?.schema?.properties ?? {})));
}

console.log("\n" + "=".repeat(46));
console.log(FAIL.length === 0 ? `  全部通过（${PASS.length} 项）`
  : `  ${FAIL.length} 项失败: ${FAIL.join(", ")}`);
console.log("=".repeat(46));
process.exit(FAIL.length === 0 ? 0 : 1);
