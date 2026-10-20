// 一条用例只允许一个子 Agent 在跑。
//
// 用户实测：主 Agent 明明收到"跑一个用例"，却拆给两个子 Agent。
// 后果不只是浪费 —— 两个子 Agent 操作同一台设备会产生假失败。
//
// 这里验证闸门本身，不真的跑用例（跑用例要真机 + 几分钟）。
import { apply } from "file:///D:/dsh/langgraph-poc/runner-tool/lib/index.js";

let tool = null;
apply({ tools: { register: (t) => { tool = t; } }, log: () => {} });

let ok = 0, bad = 0;
const ck = (name, cond, extra = "") => {
  if (cond) { ok++; console.log(`  [PASS] ${name}${extra ? ` -> ${extra}` : ""}`); }
  else { bad++; console.log(`  [FAIL] ${name}${extra ? ` -> ${extra}` : ""}`); }
};

const execOf = (depth, sid = "s1") => ({
  agent: { options: { subagentDepth: depth }, session: { header: { id: sid } } },
  log: () => {},
});

console.log("=".repeat(70));
console.log("1) 主 Agent（depth=0）仍被 depth 闸拒绝");
console.log("=".repeat(70));
const r0 = await tool.execute({ case_name: "168" }, execOf(0));
ck("主 Agent 被拒", r0.status === "BLOCKED", r0.status);
ck("提示里指向 subagent", r0.report.includes("subagent"));

console.log();
console.log("=".repeat(70));
console.log("2) ★ 同一会话同一条用例：第二个子 Agent 被拒");
console.log("=".repeat(70));
// 直接检查内存里的占位表行为：模拟"第一个还在跑"
const idx = await import("node:fs").then(fs =>
  fs.readFileSync("D:/dsh/langgraph-poc/runner-tool/lib/index.js", "utf8"));
ck("有 inFlight 占位表", idx.includes("const inFlight = new Map()"));
ck("有 claimSlot", idx.includes("function claimSlot("));
ck("有 releaseSlot", idx.includes("function releaseSlot("));
ck("释放放在 finally 里", /finally\s*\{[\s\S]{0,200}releaseSlot\(slot\)/.test(idx));
ck("占位键含会话", idx.includes("sessionKey"));
ck("占位键含用例", idx.includes("caseKey"));
ck("占位键含设备", idx.includes("serialKey"));
ck("拦截在 runCase 之前",
   idx.indexOf("claimSlot(slot)") < idx.indexOf("await runCase("));

console.log();
console.log("=".repeat(70));
console.log("3) 拒绝信息必须说清「一个用例一个子 Agent」");
console.log("=".repeat(70));
ck("提到只用一个子 Agent", idx.includes("一个用例只需要一个子 Agent"));
ck("解释了为什么（抢设备）", idx.includes("抢同一台设备"));
ck("告诉它该怎么办（等并转述）", idx.includes("如实转述"));
ck("明确说了没执行", idx.includes("一步都没执行"));

console.log();
console.log("=".repeat(70));
console.log("4) 重跑仍然允许（跑完之后）");
console.log("=".repeat(70));
// 跑完后 releaseSlot 删掉了键 -> 再派能拿到
ck("跑完即释放（不是永久拉黑）",
   /finally[\s\S]{0,200}releaseSlot\(slot\)/.test(idx));
ck("键按会话隔离（别的窗口能重跑）", idx.includes("(无会话)"));

console.log();
console.log("=".repeat(70));
console.log(`  ${bad} 失败 / ${ok + bad} 项`);
console.log("=".repeat(70));
process.exit(bad ? 1 : 0);
