/**
 * 验证 toolFilter.allow 的真实语义 —— 复刻 dsh-tools 的 view()/restrict() 算法，
 * 用**真实**的 ScopedLayers + createScope，而不是我臆想的分层。
 *
 * 依据（dsh-tools/lib/index.js）：
 *   view(scope):                                        // 2854-2879
 *     layers    = chainLayers(scope)                    // 祖先链
 *     own       = peek(scope)                           // 自己那层
 *     inherited = global + 各祖先层（**跳过 own**）
 *     restrictableNames = inherited 的全部名字          // ← allow 只能写这些
 *     visible   = inherited 中每层都 admits 的  +  own 的全部
 *
 *   restrict(filter):                                   // 2790-2805
 *     allow/deny 都转成 Set，append 到 scope 的 restrictions
 *     allow/deny 里的名字必须在 restrictableNames 里，否则抛 unknown tool
 *     "Restrictions intersect; scoped registrations remain visible"
 *
 * 要证明的三件事：
 *   Q1 allow 能把工具面收窄到只剩列出的
 *   Q2 allow 里写 "skill" 合法（skill 在祖先层 → 属于 restrictableNames）
 *   Q3 子 Agent **自己那层**注册的工具不会被误杀（否则没有答复通道）
 */
import { pathToFileURL } from "node:url";

const PKG = "C:/Users/leichao4/AppData/Local/Volta/tools/image/packages/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai";
const load = (p) => import(pathToFileURL(`${PKG}/${p}`).href);

const { ScopedLayers, createScope, scopeChainOf, scopeOf, scopeParentOf } =
  await load("dsh-scope/lib/index.js");
const { Context } = await load("cordis/lib/index.js");

const PASS = [], FAIL = [];
const check = (n, ok, d = "") => {
  (ok ? PASS : FAIL).push(n);
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${n}${d ? `  -> ${d}` : ""}`);
};

// ── 真实分层对象 ──────────────────────────────────────────────
// ScopedLayers 的构造签名是 (createLayer, onChange) —— createLayer 由调用方提供，
// 所以这里传入我们自己的建层函数（复刻 dsh-tools 里 createLayer 的最小可用子集）。
const makeLayer = (scope) => {
  const l = {
    scope,
    tools: new Map(),
    restrictions: [],
    admits(name) {
      for (const r of this.restrictions) {
        if (r.allow && !r.allow.has(name)) return false;
        if (r.deny && r.deny.has(name)) return false;
      }
      return true;
    },
  };
  // 全局层（scope===undefined）默认放行一切
  if (scope === undefined) l.admits = () => true;
  return l;
};
const layers = new ScopedLayers(makeLayer);

// host 全局层：与具体 agent 无关的工具
const HOST = ["web_fetch", "web_search", "todo_write", "goal", "ask_user_question"];
// preset 层（祖先）：父方工具面 —— 危险工具都在这里
const PRESET = ["pwsh", "write", "edit", "workflow", "ralph", "subagent",
                "subagent_fork", "skill", "read", "glob", "grep", "android_case"];
// 子 Agent 自己那层：委派运行时注册的结构化输出工具
const CHILD_OWN = ["__structured_output__"];

// 手工装配 layers 的 global / scoped。
// ScopedLayers.createLayer(scope) 是构造时注入的，实例上可取，
// 所以用 layers.createLayer() 建层、直接塞进 scoped 表 —— 与 effect() 的做法一致。
const glob = layers.global;
glob.tools = new Map(HOST.map((n) => [n, { name: n }]));

// scope key 必须是**对象**（内部用 WeakMap 存父子关系），字符串不行。
const rootCtx = new Context();
const parentKey = {};
const childKey = {};
const parentScope = createScope(rootCtx, parentKey).ctx;
// options.parent 传父 scope 对象本身
const childScope = createScope(rootCtx, childKey,
  { parent: parentScope }).ctx;

// ★ 关键：chainLayers 用的父键是 bindScopeParent 自己派生的那个对象，
//   不是我们传给 createScope 的 key。必须用 scopeParentOf(childKey) 取回来，
//   否则父层根本不在链上（踩过：子 Agent 一个父方工具都继承不到）。
const parentLayerKey = scopeParentOf(childKey);

layers.scoped.set(parentLayerKey, layers.createLayer(parentLayerKey));
layers.scoped.set(childKey, layers.createLayer(childKey));

// 子 Agent 自己那层：委派运行时注册的结构化输出工具
layers.peek(childKey).tools.set("__structured_output__",
  { name: "__structured_output__" });

// preset 层挂在父键上（子 Agent 继承父方工具面）
layers.peek(parentLayerKey).tools = new Map(
  PRESET.map((n) => [n, { name: n }]));

/** 忠实复刻 view()。入参是 scope key（chainLayers/peek 都按 key 索引）。 */
function view(scopeKey) {
  const chain = layers.chainLayers(scopeKey);
  const own = layers.peek(scopeKey);
  const inherited = new Map(layers.global.tools.entries());
  for (const l of chain) {
    if (l === own) continue;
    for (const [n, d] of l.tools.entries()) inherited.set(n, d);
  }
  const visible = new Map();
  const restrictableNames = new Set();
  for (const [n, d] of inherited) {
    restrictableNames.add(n);
    if (chain.every((l) => l.admits(n))) visible.set(n, d);
  }
  if (own) for (const [n, d] of own.tools.entries()) visible.set(n, d);
  return { visible, restrictableNames };
}

/** 忠实复刻 restrict() 的校验与效果。 */
function restrict(scope, filter) {
  const { restrictableNames } = view(scope);
  const names = [...(filter.allow ?? []), ...(filter.deny ?? [])];
  const unknown = names.filter((n) => !restrictableNames.has(n));
  if (unknown.length) throw new Error(`unknown global tool(s): ${unknown.join(", ")}`);
  const compiled = {};
  if (filter.allow) compiled.allow = new Set(filter.allow);
  if (filter.deny) compiled.deny = new Set(filter.deny);
  layers.peek(scope).restrictions.push(compiled);
}

console.log("=== 0. 过滤前的工具面（子 Agent 继承到多少）===");
const before = [...view(childKey).visible.keys()].sort();
console.log(`  ${before.length} 个: ${before.join(", ")}`);
check("过滤前继承到 pwsh（危险）", before.includes("pwsh"));
check("过滤前继承到 workflow（危险）", before.includes("workflow"));

console.log("\n=== 1. 施加 toolFilter.allow ===");
const ALLOW = ["android_case", "skill", "read", "glob", "grep"];
try {
  restrict(childKey, { allow: ALLOW });
  check("restrict({allow}) 不抛错", true);
} catch (e) {
  check("restrict({allow}) 不抛错", false, e.message);
}

const after = [...view(childKey).visible.keys()].sort();
console.log(`  剩下 ${after.length} 个: ${after.join(", ")}`);

console.log("\n=== 2. Q1 危险工具确实被挡掉 ===");
for (const bad of ["pwsh", "write", "edit", "workflow", "ralph",
                   "subagent", "subagent_fork"]) {
  check(`${bad} 被挡掉`, !after.includes(bad));
}

console.log("\n=== 3. Q2 allow 里写 skill 合法（settable 名字包含祖先层工具）===");
const names = view(childKey).restrictableNames;
check("restrictableNames 含 skill", names.has("skill"));
check("restrictableNames 含 android_case", names.has("android_case"));
check("skill 被保留", after.includes("skill"));
check("android_case 被保留", after.includes("android_case"));

console.log("\n=== 4. Q3 子 Agent 自己那层不被误杀 ===");
check("__structured_output__ 仍可见（答复通道没断）",
  after.includes("__structured_output__"),
  after.includes("__structured_output__") ? "" : "被误杀了！");

console.log("\n=== 5. 未知名字会抛错（所以 allow 必须写对）===");
try {
  restrict(childKey, { allow: ["nonexistent_tool"] });
  check("未知名字抛错", false, "居然没抛");
} catch (e) {
  check("未知名字抛错", /unknown/i.test(e.message), e.message.slice(0, 60));
}

console.log("\n" + "=".repeat(52));
console.log(FAIL.length === 0 ? `  全部通过（${PASS.length} 项）`
  : `  ${FAIL.length} 项失败: ${FAIL.join(", ")}`);
console.log("=".repeat(52));
process.exit(FAIL.length === 0 ? 0 : 1);
