// 真实验证：同一会话同时派两个子 Agent 跑同一条用例。
// 第一个应该跑起来，第二个应该被立刻拒绝。
import { apply } from "file:///D:/dsh/langgraph-poc/runner-tool/lib/index.js";

let tool = null;
apply({ tools: { register: (t) => { tool = t; } }, log: () => {} });

const exec1 = {
  agent: { options: { subagentDepth: 1 }, session: { header: { id: "sess-X" } } },
  log: () => {},
};
const exec2 = {
  agent: { options: { subagentDepth: 1 }, session: { header: { id: "sess-X" } } },
  log: () => {},
};

console.log("同一会话，同时派两个子 Agent 跑 168：\n");
const t0 = Date.now();
const pa = tool.execute({ case_name: "168" }, exec1);
// 稍等一下确保第一个已占位
await new Promise((r) => setTimeout(r, 300));
const pb = tool.execute({ case_name: "168" }, exec2);

const b = await pb;
const tb = ((Date.now() - t0) / 1000).toFixed(1);
console.log(`第二个（应被拒）用时 ${tb}s：`);
console.log(b.report.split("\n").slice(0, 6).map((l) => "  " + l).join("\n"));
console.log();

const a = await pa;
const ta = ((Date.now() - t0) / 1000).toFixed(1);
console.log(`第一个（应跑完）用时 ${ta}s：status=${a.status}`);

const okA = a.status === "PASS" || a.status === "FAIL";
const okB = b.status === "BLOCKED" && Number(tb) < 5;

console.log();
console.log(`  ${okB ? "[PASS]" : "[FAIL]"} 第二个被立刻拒绝（${tb}s，不是排队跑了 60+s）`);
console.log(`  ${okA ? "[PASS]" : "[FAIL]"} 第一个正常跑完（${a.status}）`);
process.exit(okA && okB ? 0 : 1);
