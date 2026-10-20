/** 用**真实** dsh-tools 验证 render 层：原 bug 会复现，我们的插件不会。 */
import { pathToFileURL } from "node:url";

const DSH = "C:/Users/leichao4/AppData/Local/Volta/tools/image/packages/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai";
const { defineTool } = await import(pathToFileURL(DSH + "/dsh-tools/lib/index.js").href);

let pass = 0, fail = 0;
const check = (name, ok, extra = "") => {
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${name}${extra ? "  -> " + extra : ""}`);
  ok ? pass++ : fail++;
};

console.log("--- 1) 复现原 bug：只给 schema，不给 render ---");
// parameters 是 DSH 自己的**属性表**格式，不是原始 JSON Schema
const bad = defineTool({
  name: "bad_tool",
  description: "x",
  parameters: {
    s: { type: "string", description: "x" },
  },
  output: {
    schema: {
      type: "object",
      additionalProperties: false,
      properties: { s: { type: "string", required: true } },
    },
  },
  execute: async () => ({ s: "hi" }),
});
let bugReproduced = false;
try {
  bad.output.render({}, { s: "hi" });
} catch (e) {
  bugReproduced = /userRender is not a function/.test(String(e.message));
  console.log(`        抛错原文: ${String(e.message).split("\n")[0]}`);
}
check("原 bug 已复现（证明这是 harness 的行为，不是我们编的）", bugReproduced);

console.log("\n--- 2) 我们的 android_case 插件 ---");
const mod = await import(pathToFileURL("D:/dsh/langgraph-poc/runner-tool/lib/index.js").href);
let reg = null;
mod.apply({ tools: { register: (t) => { reg = t; } } });
check("插件加载并注册成功", reg?.name === "android_case", String(reg?.name));
let rendered = null;
try {
  rendered = reg.output.render({}, { status: "PASS", engine: "model",
                                      report: "用例结论摘要" });
} catch (e) {
  console.log(`        抛错: ${e.message}`);
}
// ★ 契约是 ContentBlock[]（数组），**不是字符串**。
//   字符串在普通会话能显示，但测试模式（PTC）会炸：
//     content.some is not a function
//   （dsh-tools/lib/types/ptc.js:458）
check("render 返回数组（ContentBlock[]）", Array.isArray(rendered),
      JSON.stringify(rendered));
check("render 把 report 带出来了",
      Array.isArray(rendered) &&
        rendered.some((b) => b.type === "text" &&
                             String(b.text).includes("用例结论摘要")),
      JSON.stringify(rendered));

console.log(`\n  ${fail ? fail + " 项失败" : "全部通过"}（${pass + fail} 项）`);
process.exit(fail ? 1 : 0);
