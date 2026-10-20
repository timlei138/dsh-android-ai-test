/** 主 Agent 不许直接跑用例 —— 只有执行子 Agent 能调 android_case。
 *
 * 背景（用户实测发现）：
 *     主 Agent 手上也挂着 android_case，它看到就顺手直接调，
 *     `android_case · 168` 一步到位，**子 Agent 根本没被创建**。
 *     三层设计（主 Agent 拆解 → 子 Agent 执行 → 执行器兜底）退化成一层。
 *
 * 为什么不能"干脆不给主 Agent 挂这个工具"：
 *     子 Agent 的工具面是**继承**父 Agent 再收窄的
 *     （dsh-subagent: childOptions = { ...parent.options }），没有
 *     "给子 Agent 单独挂插件"的机制。主 Agent 不挂 → 子 Agent 也继承不到 →
 *     toolFilter.allow 里的 android_case 会抛 "names unknown global tool"。
 *
 * 所以闸门必须在运行时：插件判断调用方的 delegationDepth。
 *     0 = 主 Agent → 拒绝
 *     1 = 子 Agent → 放行
 *
 * delegationDepth 持久化且单调（模型改不了），因此这个判断**不是提示词约束**。
 */
import { pathToFileURL } from "node:url";

const F = "D:/dsh/langgraph-poc/runner-tool/lib/index.js";

let pass = 0, fail = 0;
function check(name, cond, extra = "") {
  if (cond) { pass++; console.log(`  [PASS] ${name}` + (extra ? `  -> ${extra}` : "")); }
  else { fail++; console.log(`  [FAIL] ${name}` + (extra ? `  -> ${extra}` : "")); }
}

const mod = await import(pathToFileURL(F).href + "?v=depth-" + Date.now());
let tool = null;
mod.apply({ tools: { register: (t) => { tool = t; }, defineTool: (t) => t } });

const GATE = "只能由「执行子 Agent」调用";
const hitGate = (r) => String(r?.report ?? "").includes(GATE);

console.log("=== 1) ★ 主 Agent（depth=0）必须被拒 ===");
const r0 = await tool.execute(
  { case_name: "168" }, { agent: { options: { subagentDepth: 0 } } });
check("主 Agent 被闸门拦下", hitGate(r0), String(r0?.status));
check("status 是 BLOCKED", r0?.status === "BLOCKED", String(r0?.status));

console.log("\n=== 2) 拒绝时要说清「该怎么改」（不然模型会原地重试）===");
const msg = String(r0?.report ?? "");
check("说明你是主 Agent", /主 Agent/.test(msg));
check("给出正确写法 subagent", /subagent\s*\(/.test(msg));
check("说明为什么（避免绕过）", /执行器|纪律/.test(msg));

console.log("\n=== 3) ★ 子 Agent（depth=1）必须放行 ===");
// 用一个不存在的 case，让它进入执行器后自然报错 —— 但不能是闸门拦的
const r1 = await tool.execute(
  { case_name: "___definitely_not_a_case___" },
  { agent: { options: { subagentDepth: 1 } } });
check("子 Agent 没被闸门拦", !hitGate(r1),
      hitGate(r1) ? "被误拦！" : "已放行到执行器");

console.log("\n=== 4) depth 来自 session.header 时也要认 ===");
// delegationDepth 的权威来源是 session.header.delegationDepth
const r2 = await tool.execute(
  { case_name: "168" },
  { agent: { session: { header: { delegationDepth: 0 } } } });
check("header.delegationDepth=0 也被拦", hitGate(r2), String(r2?.status));

console.log("\n=== 5) 拿不到 depth 时不误伤（放行，交给执行器自己判断）===");
// exec.agent 缺失是宿主实现细节变化，不该把所有人锁死
const r3 = await tool.execute(
  { case_name: "___definitely_not_a_case___" }, {});
check("无 agent 信息时不被闸门拦", !hitGate(r3),
      hitGate(r3) ? "误伤了" : "放行");

console.log("\n=== 6) 闸门在 execute 最前面（不能跑了一半才拒）===");
// 否则 devices 已被碰过、台账已写，拒绝就没意义了
const src = (await import("node:fs")).readFileSync(F, "utf8");
const iExec = src.indexOf("async execute(args, exec)");
const iGate = src.indexOf(GATE);
// ⚠️ 别用 indexOf("runCase(spec") —— 注释里也提到过，会匹配到文件前部。
//    用真正的调用形态：`await runCase(spec,`
const iRun = src.indexOf("await runCase(spec,");
check("闸门在 execute 内", iExec !== -1 && iGate > iExec);
check("闸门在调用执行器之前", iGate !== -1 && iRun !== -1 && iGate < iRun,
      `gate@${iGate} < runCase@${iRun}`);

console.log("\n=== 7) ★ 不设墙钟超时（子 Agent 执行不应该有时间限制）===");
//
// 用户原话："子 Agent 的执行不应该有时间限制。"
//
// 曾经试过声明 timeoutMs: 600_000 想"把默认 60s 抬高"，方向是错的：
//   ① DSH 的工具注册只校验并保存 timeoutMs（dsh-tools:2778），
//      工具执行链路里**没有任何计时器**（deadline/setTimeout 零匹配）—— 设了也不生效；
//   ② 就算生效，那只是把"猜一个够大的数字"换个位置：
//      用例一变长、设备一变慢，同一个坑还会再踩。
//
// 兜底交给执行器，而且按**进度**而不是按墙钟：
//   max_loops(80 轮) / 同点连点 / 交替循环 / 崩溃硬终止(exit 3)
// 这些比超时准 —— 它知道自己在打转，而不是只知道"过了很久"。
check("android_case 不声明 timeoutMs", tool.timeoutMs === undefined,
      tool.timeoutMs === undefined ? "未声明（正确）" : `${tool.timeoutMs}ms`);

console.log("\n=== 8) ★ 子 Agent 必须有独立 persona（否则它以为自己是主 Agent）===");
//
// 这是 `subagent run failed` 的真因：
//   applyChildComposition() 里 composeFrom() 让子 Agent 完整加入父的 preset
//   （所以能继承 android_case），但 persona 走另一条路 ——
//   只有 tool-subagent 的 config.persona 非 undefined 才覆盖
//   `deployment:persona-prefix` 那一段。
//   不写 ⇒ 子 Agent 读到父的"你负责拆解与派发" ⇒ 想把任务再派一层
//   ⇒ maxDepth:1 不许 ⇒ 那轮 turn 非常规结束 ⇒ "subagent run failed"（无 Diagnostic）。
const yamlPath = "C:/Users/leichao4/.dsh/.agent-presets/android-runner/agent.cordis.yml";
const yamlSrc = (await import("node:fs")).readFileSync(yamlPath, "utf8");
// 轻量断言：不引入 YAML 解析器，只看结构
const subIdx = yamlSrc.indexOf("id: tool-subagent");
check("找得到 tool-subagent 节点", subIdx !== -1);
const tail = yamlSrc.slice(subIdx);
check("tool-subagent 配了 persona", /^\s{4}persona:\s*>-/m.test(tail),
      /^\s{4}persona:\s*>-/m.test(tail) ? "有" : "★ 没有 —— 子 Agent 会继承父的职责描述");
check("persona 说清是「执行子 Agent」", tail.includes("执行子 Agent"));
check("persona 明确否掉「派发」", tail.includes("不是派发") || tail.includes("别再派"));
check("allow 里不含 subagent（不能继续下派）",
      !/allow:\s*\n(?:\s*-\s*\w+\s*\n)*\s*-\s*subagent/m.test(tail));

console.log(`\n${"=".repeat(52)}`);
console.log(`  ${fail ? `${fail} 项失败` : "全部通过"}（${pass + fail} 项）`);
console.log("=".repeat(52));
process.exit(fail ? 1 : 0);
