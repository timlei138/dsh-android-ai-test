/**
 * `android_case` —— 跑一条 Android GUI 测试用例的唯一入口。
 *
 * 设计意图（用户要求）：
 *   "子 Agent 默认继承父方的工具集 这个需要限制下。让其能够只用我们提供的方法。"
 *
 * 所以这个工具是子 Agent **唯一**能碰设备的门。它不接受 shell 命令、不接受文件路径、
 * 不接受坐标 —— 只接受"一条用例长什么样"，然后交给 LangGraph 骨架执行。
 * 子 Agent 的模型只负责**判断**（哪一步到位了、该点哪、异常怎么办），
 * 流程节奏、落点判定、结论闸门、坐标合法性都由骨架控制。
 *
 * 为什么不用 shell 工具跑 python：
 *   · shell 能跑任意命令 —— 子 Agent 可以绕开骨架直接调 adb，纪律形同虚设
 *   · 输出是自由文本，没有结构化报告，主 Agent 拿不到可校验的结论
 *   · 参数拼错没有任何保护
 *
 * 这个工具把上述三点都堵死：固定入口、结构化入参、结构化出参。
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { defineTool } from "@deepseek-ai/dsh-tools";

const name = "tool-android-case";
const inject = ["tools"];

/** 用例执行骨架所在目录（LangGraph POC）。可用环境变量覆盖。 */
const RUNNER_DIR =
  process.env.ANDROID_CASE_DIR || "D:\\dsh\\langgraph-poc";

/**
 * 跑 run_case_json.py 的解释器。
 *
 * ⚠️ 这里**必须是 `langgraph-poc/.venv`（编排 venv）**，不是 assets/.venv。
 *    工作区里有两个 venv，依赖完全不同、不能互换：
 *      · langgraph-poc/.venv        → langgraph / langchain / pyyaml  ← 本文件要用的
 *      · langgraph-poc/assets/.venv → adbutils / opencv / rapidocr（设备操作）
 *
 *    为什么是编排 venv：`run_case_json.py` 会 import `subagent`，
 *    而 subagent → child_agent → **langgraph**。langgraph 只在编排 venv 里。
 *    设备依赖（adbutils）不在这里用 —— 它由 Python 侧
 *    `assets_path.runner_python()` 独立解析，喂给 `tools/run.py`（真正碰设备的那个）。
 *
 *    ⚠️ 别"顺手"把它改成 assets/.venv：那会让 run_case_json.py 直接
 *    ImportError: No module named 'langgraph'（实测确认过两个 venv 的依赖清单）。
 *    "看见 adbutils 在 assets/.venv 就以为设备操作要用它" 是这里最容易犯的错。
 */
const RUNNER_PY =
  process.env.ANDROID_CASE_PYTHON ||
  join(RUNNER_DIR, ".venv", "Scripts", "python.exe");

/**
 * 设备技能包（知识卡/工具层的来源）。
 *
 * ⚠️ 留空时**交给 Python 侧解析**（assets_path.skill_root()），
 *    因为资产已经迁到工作区的 assets/ 下，再由 Node 写死老路径
 *    就会"Node 读旧的、Python 读新的" —— 两边不一致且**不会报错**。
 *    只有显式设了 ANDROID_SKILL_ROOT 才在这里覆盖。
 */
const SKILL_ROOT = process.env.ANDROID_SKILL_ROOT || "";

/**
 * 跑一次用例，返回结构化报告。
 *
 * 走 `run_case_json.py` —— 一个薄 CLI：读 stdin 的用例 JSON，
 * 调 subagent.run_case()，把 SubagentReport 以 JSON 打到 stdout。
 * 这样 Node 侧不需要懂 Python，也不需要解析自由文本。
 */
function runCase(spec, signal) {
  return new Promise((resolve, reject) => {
    if (!existsSync(RUNNER_PY)) {
      reject(new Error(`找不到执行器 python：${RUNNER_PY}`));
      return;
    }
    const child = spawn(RUNNER_PY, [join(RUNNER_DIR, "run_case_json.py")], {
      cwd: RUNNER_DIR,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    });

    let out = "";
    let err = "";
    child.stdout.on("data", (d) => {
      out += d.toString("utf8");
    });
    child.stderr.on("data", (d) => {
      err += d.toString("utf8");
    });
    child.on("error", reject);
    child.on("close", (code) => {
      // 报告本身走 stdout；进度日志走 stderr。
      // 退出码非 0 不代表没有报告 —— 用例 FAIL 也会带报告出来，
      // 只有"连报告都没有"才算执行失败。
      const text = out.trim();
      if (!text) {
        reject(new Error(`执行器无输出（exit=${code}）\n${err.slice(-2000)}`));
        return;
      }
      try {
        resolve(JSON.parse(text));
      } catch {
        reject(new Error(`执行器输出不是 JSON（exit=${code}）：${text.slice(0, 800)}`));
      }
    });

    if (signal) {
      signal.addEventListener("abort", () => child.kill(), { once: true });
    }
    child.stdin.end(JSON.stringify(spec), "utf8");
  });
}

/** 把报告压成模型能直接读的短文本 —— 不把整份 JSON 塞回上下文。 */
function render(report) {
  const L = [];
  L.push(`结论：${report.status}`);
  L.push(`概要：${report.summary}`);
  L.push(`步骤：${report.steps_done}  覆盖达标：${report.coverage_ok ? "是" : "否"}`);
  // ★ 引擎必须放在**最前面几行**，而且 autopilot 要显眼。
  // 为什么：两者的报告长得一模一样，只有这里能分辨。
  // 把 autopilot 的成绩当成模型的成绩，是实测踩过的坑。
  if (report.engine === "autopilot") {
    L.push("⚠️ 引擎：autopilot（规则应答器，**没有调用模型**）");
    L.push("   ⇒ 这份结论只说明「YAML 结构能跑通」，**不能当作用例的真实结论**。");
    L.push("   ⇒ 要真实结论请重跑：android_case(..., answer=\"model\")。");
  } else if (report.engine === "model") {
    L.push("引擎：model（真模型应答，结论有效）");
  }
  if (report.issues?.length) {
    L.push("问题：");
    for (const i of report.issues) L.push(`  - ${i}`);
  }
  if (report.findings?.length) {
    L.push("预期结果：");
    for (const f of report.findings) {
      const tag = `${f.id ?? f.step ?? "?"} ${f.status}`;
      L.push(`  [${tag}] ${f.desc ?? ""}`);
      if (f.actual) L.push(`      实际：${String(f.actual).slice(0, 300)}`);
    }
  }
  // 授权留痕必须回给主 Agent —— 自动点"允许"改了设备权限状态，不可逆。
  if (report.runtime_permission_prompts?.length) {
    L.push(`运行时权限（工具层自动授予，共 ${report.runtime_permission_prompts.length} 个）：`);
    for (const p of report.runtime_permission_prompts) L.push(`  - ${p.prompt}`);
  }
  if (report.dialogs_dismissed?.length) {
    L.push("App 内弹窗（属于流程的一部分）：");
    for (const d of report.dialogs_dismissed) L.push(`  - ${d.text}`);
  }
  if (report.restored?.length) L.push(`已恢复：${report.restored.join("；")}`);
  if (report.report_path) L.push(`报告文件：${report.report_path}`);
  return L.join("\n");
}

const apply = (ctx) => {
  const tool = defineTool({
      name: "android_case",
      // ★★ 不声明 timeoutMs —— 由执行器自己管时间，不要给工具套超时。
      //
      // 一条真用例要多久？取决于设备、步数、模型响应：
      //     168（4 步）实测 102s / 269s（两次），慢设备还会更久。
      // 这是**正常**的，不是异常。
      //
      // 曾经试过声明 timeoutMs: 600_000 想"把默认 60s 抬高"，那是错的方向：
      //   ① DSH 的工具注册只**校验并保存** timeoutMs（dsh-tools/lib/index.js:2778），
      //      工具执行链路里没有任何计时器 —— 设了也不生效；
      //   ② 就算生效，那也只是把"猜一个够大的数字"换个位置，
      //      用例一变长、设备一变慢，同一个坑还会再踩。
      //
      // 所以这里什么都不设。真正的兜底在执行器里，而且是**按进度**而不是按墙钟：
      //   · max_loops（默认 80 轮）—— 轮次烧光就停
      //   · 同点连点 / 交替循环识别 —— 卡住了就停
      //   · 崩溃硬终止（exit 3）—— 一次就停
      // 这些判据比"超时"准：它知道自己在原地打转，而不是只知道"过了很久"。
      //
      // 用户原话："子 Agent 的执行不应该有时间限制。"
      description: [
        "在真实 Android 设备上执行一条 GUI 测试用例，返回结构化结论。",
        "",
        "这是你操作设备的**唯一**途径 —— 你没有 shell、没有文件读写、没有 adb。",
        "你只负责把用例描述清楚；执行节奏、落点判定、坐标合法性由执行器负责。",
        "",
        "用例格式：steps 每步给 desc（做什么，说人话）+ landing（**可验证的落点判据**）。",
        "landing 必须是 activity=xxx / rid=xxx / text=xxx，多个用逗号(AND)或竖线(OR)。",
        "landing 写不准会导致用例判错 —— 这是最重要的一件事。",
        "",
        "asserts 是验收预期，check 用同样的判据语法。",
        "knowledge_files 可选：列设备技能包里的知识卡路径（如 knowledge/xxx.md），",
        "执行器会把内容提供给执行层参考。",
      ].join("\n"),
      parameters: {
        case_name: {
          type: "string",
          description: [
            "★ 推荐用法：直接跑已经沉淀好的用例 YAML（旧 SKILLS 的 storage/cases/*.yaml），",
            "填文件名片段即可，如 \"168\"。填了这个就**不用**再填 steps/asserts ——",
            "加载器会自己读步骤、判据、前提(pm clear)、每步的坑。",
            "不知道有哪些用例就先看 skill 里的 cases 目录。",
          ].join("\n"),
        },
        case_id: {
          type: "string",
          description: "用例编号，如 168。用 case_name 时可留空（从 YAML 读）。",
        },
        title: {
          type: "string",
          description: "用例标题。用 case_name 时可留空。",
        },
        package: {
          type: "string",
          description: "被测 App 包名，如 com.zui.calendar。用 case_name 时可留空。",
        },
        steps: {
          type: "array",
          description: "操作步骤，按顺序执行。用 case_name 时可省略。",
          items: {
            type: "object",
            additionalProperties: false,
            properties: {
              id: { type: "string", required: true, description: "步骤号，如 s1" },
              desc: { type: "string", required: true, description: "这一步做什么" },
              landing: {
                type: "string",
                description: "落点判据（activity=/rid=/text=），用于判断本步是否到位",
              },
              locate: {
                type: "string",
                description: [
                  "这一步该点哪个控件的**定位提示**，如 rid=iv_more 或 text=课程表。",
                  "★ 很重要：图标类按钮（更多/返回/加号）通常**没有文字**，",
                  "模型光看可点列表认不出它是哪个，会去点错的东西。",
                  "用 desc 里的说法找到对应 rid 后填在这里。",
                ].join("\n"),
              },
              pitfalls: {
                type: "array",
                items: { type: "string" },
                description: "这一步已知的坑，会提示给执行层",
              },
            },
          },
        },
        asserts: {
          type: "array",
          // ⚠️ 这里**不能**写 required: true。
          //
          // defineTool 会把参数对象里每个 `required: true` 收集成根级
          // JSON Schema 的 `required: [...]`（dsh-tools lib/types/schema.js L81-82）。
          // 于是 harness 在**进入 execute 之前**就强制要求 asserts 存在，
          // 而 execute 的第一行恰恰是 `args.case_name ? {只传 case_name...}` —— 
          // 走 YAML 的分支**本来就不该填 asserts**。
          //
          // 结果：`android_case(case_name="168")` 被 harness 拦下，
          // 报 "missing required property asserts"（外加 steps/title/... 一串），
          // **execute 一次都没跑**，插件里那段 case_name 分支成了死代码。
          // 这与 case_name 自己的说明（"填了这个就不用再填 steps/asserts"）直接矛盾。
          //
          // 所以：断言是否必填由 execute 按入口分支去判，不在 schema 层一刀切。
          description: "验收预期，逐条判定 PASS/FAIL。用 case_name 跑 YAML 时可省略（从 YAML 读）。",
          items: {
            type: "object",
            additionalProperties: false,
            properties: {
              id: { type: "string", required: true, description: "预期号，如 a1" },
              desc: { type: "string", required: true, description: "这条预期验什么" },
              check: {
                type: "string",
                required: true,
                description: [
                  "判据。两种写法：",
                  "  · 字符串：activity=Foo,rid=bar（逗号=且，竖线=或）",
                  "  · 用 case_name 跑 YAML 时，YAML 里的**结构化判据**",
                  "    （activity/node_present/node_text/dialog_present/package）",
                  "    会被代码直接判定，判不了才交回模型。",
                ].join("\n"),
              },
            },
          },
        },
        serial: {
          type: "string",
          description: "设备序列号；留空则自动选用唯一在线设备",
        },
        knowledge_files: {
          type: "array",
          items: { type: "string" },
          description: "知识卡相对路径，如 knowledge/com.zui.calendar.md",
        },
        preconditions: {
          type: "array",
          description: [
            "执行前必须做的前提操作，**以及执行后的恢复操作**。",
            "kind=data 表示会改设备数据（如 pm clear / 改系统设置），",
            "必须在 undo 里给出怎么恢复 —— 不写 undo 等于留下脏状态。",
            "kind=env 表示环境/硬件前提（如飞行模式、屏幕方向），",
            "undo 里写怎么还原。",
            "例：{kind:'data', desc:'清掉旧课程表', do:['pm clear com.x'], undo:[]}",
          ].join("\n"),
          items: {
            type: "object",
            additionalProperties: false,
            properties: {
              kind: {
                type: "string",
                required: true,
                enum: ["data", "env"],
                description: "data=改数据（要能恢复） env=改环境（要能还原）",
              },
              desc: { type: "string", description: "为什么需要这个前提" },
              do: {
                type: "array",
                required: true,
                items: { type: "string" },
                description: "要执行的 shell 命令（如 pm clear com.x）",
              },
              undo: {
                type: "array",
                items: { type: "string" },
                description: "跑完怎么恢复；不需要恢复就留空数组",
              },
            },
          },
        },
        perm_action: {
          type: "string",
          enum: ["grant", "deny"],
          description:
            "运行时权限策略；留空=默认同意（工具层自动点，抢 6 秒窗口）。只有测「拒绝分支」才填 deny。",
        },
        writeback: {
          type: "boolean",
          description: [
            "跑完是否把**实际走过的路径**回写进用例 YAML（默认 true）。",
            "只在 case_name/有 YAML 文件时生效 —— 现场拆的用例没有文件可写。",
            "改动会出现在报告 yaml_writeback.changed 里，并逐条列出",
            "「YAML 原写 X / 实际走 Y」。",
            "",
            "⚠️ 只改 do:（怎么走），**绝不改 expect:/assertions:**（验收标准）——",
            "标准是人写的需求，路径是工具的记录，两者不能混。",
            "结论为 BLOCKED 时不回写（路径没走完，写进去会固化半截经验）。",
            "想先看效果不改文件，传 false。",
          ].join("\n"),
        },
        answer: {
          type: "string",
          enum: ["model", "autopilot"],
          description: [
            "谁来做判断。默认 model —— **跑用例不用传**。",
            "",
            "  model（默认）= 真模型：decide/assert 交给 LLM。这是「用例过不过」的唯一真实答案。",
            "  autopilot    = 规则应答器：按 locate/landing 走，完全不调用 LLM。",
            "                 只验「YAML 写对没有 / 结构通不通」，不产生用例结论。",
            "",
            "怎么选：",
            "  · 跑用例、要结论        → 不用传（默认就是 model）",
            "  · 改完 YAML 想快速自测   → 显式写 answer=\"autopilot\"（快、不花钱）",
            "",
            "⚠️ 绝对不要拿 autopilot 的结果当用例结论。",
            "两者的报告长得一模一样（都有轮次/decide/PASS），但 autopilot 根本没接模型 ——",
            "实测被这个坑过一次：跑出 decide=0，以为模型真没被叫，其实是没接。",
            "报告里的 engine 字段会标死这次用的是哪个。",
          ].join("\n"),
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            status: {
              type: "string",
              required: true,
              description: "PASS / FAIL / BLOCKED（只有这三档）",
            },
            engine: {
              type: "string",
              required: true,
              description:
                "model=真模型（结论有效）/ autopilot=规则应答器（不能当用例结论）",
            },
            report: { type: "string", required: true, description: "可读的结论摘要" },
          },
        },
        // ★ 必填，不是可选的。
        //
        // 踩过：只给 schema 不给 render，工具能注册成功，但**第一次真实调用**
        // 就炸在渲染层：
        //   dsh-tools/lib/index.js:840  const userRender = options.output.render  → undefined
        //   dsh-tools/lib/index.js:856  return userRender(args, value)            → TypeError
        // 而且 execute 其实**已经跑完并返回了** {status, report} —— harness 不读它，
        // 只认 output.render。于是设备一步没动、前置也没执行，白跑一轮。
        //
        // 同一个 defineTool 里 presentationMeta 判了空（L858），render 没判 ——
        // 因为注册校验（L2776）要求 render 必须是函数，是**这个包装函数**
        // 没兜住"用户没提供"的情况。这一侧我们改不了（只发布 lib，无源码），
        // 所以插件必须自己给 render。
        // ★ 必须返回 ContentBlock[]（数组），**不是字符串**。
        //
        // 类型定义写得很清楚（dsh-tools 的 ToolOutputDefinition）：
        //     render(args, value): ContentBlock[]
        //
        // 以前这里 `return value.report;` 返回裸字符串。普通会话能忍（只是
        // 显示得怪），但**测试模式（PTC）直接炸**：
        //     dsh-tools/lib/types/ptc.js:458
        //     if (!result.isError && result.content.some(block => block.type === 'image'))
        //     → TypeError: content.some is not a function
        //
        // 原因是 `materializeFinalResult` 把 render 的返回值**原样**当 content
        // （index.js:3422 `rendered = tool.output.render(...)` → :3466 `content: result.content`），
        // 不做任何数组包装。字符串没有 .some()，于是整轮运行失败。
        render(_args, value) {
          return [{ type: "text", text: String(value.report ?? "") }];
        },
      },
      async execute(args, exec) {
        // ★★ 只有"执行子 Agent"能调这个工具 —— 主 Agent 一律拒绝。
        //
        // 为什么要有这道闸（用户实测发现的）：
        //   主 Agent 手上也挂着 android_case，于是它看到就顺手直接调，
        //   `android_case · 168` 一步到位，**子 Agent 根本没被创建**。
        //   三层设计（主 Agent 拆解 → 子 Agent 执行 → 执行器兜底）退化成一层，
        //   子 Agent 的工具面收窄也就完全没意义了。
        //
        // 为什么不能"干脆不给主 Agent 挂这个工具"：
        //   子 Agent 的工具面是**继承**父 Agent 再收窄的
        //   （dsh-subagent: childOptions = { ...parent.options }），
        //   没有"给子 Agent 单独挂插件"的机制。
        //   主 Agent 不挂 → 子 Agent 也继承不到 → toolFilter 里的
        //   android_case 会抛 "names unknown global tool"，子 Agent 派不出去。
        //   ⇒ 工具**必须在**，只能靠运行时判断"谁在调"。
        //
        // 判据：delegationDepthOf(agent)
        //   顶层/主 Agent = 0；子 Agent = 1（subagent depth = 父 depth + 1）。
        //   agent.session.header.delegationDepth 是持久化且单调的，
        //   模型改不了；agent.options.subagentDepth 只能加深、不能降低。
        //   所以这个判断**绕不过去**（不是提示词约束）。
        //
        // 与 user 的设计一致："主 Agent 执行 webUI 相关内容 / case 拆解，
        // 子 Agent 执行运行 case"。
        const depth =
          exec?.agent?.options?.subagentDepth ??
          exec?.agent?.session?.header?.delegationDepth ??
          null;

        // 【已拆除】这里曾经有一段临时探针，把 exec.agent 的结构落盘到
        // _probe_depth.log。它的历史作用很关键，记下来免得以后重复排查：
        //
        //   用户报"子 Agent 调 android_case 就 subagent run failed"，
        //   而我加的 depth 闸门在 execute **里面**。探针日志一直是**空的**
        //   ⇒ 子 Agent 压根没走到 execute
        //   ⇒ 直接排除"闸门误判"这条路。
        //
        //   真因在别处：子 Agent 没有自己的 persona，原样继承了父的
        //   "你负责拆解与派发"，于是它以为自己是主 Agent、想把任务再往下派，
        //   而 maxDepth: 1 不许 —— 那一轮 turn 非常规结束，
        //   上层只看到 "subagent run failed"（连 Diagnostic 都没有）。
        //   修法见 agent.cordis.yml 里 tool-subagent 的 config.persona。
        if (depth !== null && Number.isSafeInteger(depth) && depth === 0) {
          const msg =
            "android_case 只能由「执行子 Agent」调用，主 Agent 不能直接跑。\n" +
            "\n" +
            "你是主 Agent。你的职责是：\n" +
            "  1. 拆解用例（steps / asserts / landing 落点判据）\n" +
            "  2. 用 subagent 工具派发给执行子 Agent\n" +
            "\n" +
            "正确写法：\n" +
            "  subagent(prompt: '执行用例 168，用 android_case(case_name=\"168\")，" +
            "把 status/engine/steps_done/issues 原样回报')\n" +
            "\n" +
            "为什么这样设计：执行节奏、坐标合法性、落点复核、死循环阻断都在子 Agent\n" +
            "那侧的执行器里。主 Agent 直接调等于把这些纪律全绕过了。";
          return {
            // 主 Agent 越权被拒 —— 用例一步都没跑，属于 BLOCKED（没执行完）。
            status: "BLOCKED",
            engine: "",
            report: msg,
          };
        }

        // ★ 两条路：跑沉淀好的 YAML（推荐），或跑现场拆的步骤。
        // 走 YAML 时 steps/asserts/package 都由加载器从文件读，
        // 这里就不传 —— 传了反而会和 YAML 打架。
        const spec = args.case_name
          ? {
              case_name: args.case_name,
              serial: args.serial ?? null,
              perm_action: args.perm_action ?? null,
              answer: args.answer ?? "model",
              // 只有走 YAML 时才可能回写（有真实文件）。传 false 可只跑不改。
              writeback: args.writeback ?? true,
              skill_root: SKILL_ROOT,
              diag: true,
            }
          : {
              case_id: args.case_id,
              title: args.title,
              package: args.package,
              steps: args.steps,
              asserts: args.asserts,
              preconditions: args.preconditions ?? [],
              knowledge_files: args.knowledge_files ?? [],
              serial: args.serial ?? null,
              perm_action: args.perm_action ?? null,
              answer: args.answer ?? "model",
              skill_root: SKILL_ROOT,
              diag: true,
            };
        const report = await runCase(spec, exec?.signal);
        return {
          status: String(report.status ?? "BLOCKED"),
          engine: String(report.engine ?? ""),
          report: render(report),
        };
      },
  });

  // ★ 自检：output.render 必须是能跑通的函数。
  //
  // 为什么需要：defineTool 会**无条件**包一层 render（dsh-tools/lib/index.js:855），
  // 哪怕用户没提供。而注册校验（同文件 L2776）只检查"是不是函数"——
  // 包装函数永远通过。于是漏写 render 的工具能正常注册，
  // 直到**第一次真实调用**才抛 TypeError。
  // 代价是：设备白跑一轮、前置条件也没执行（实测就是这么发现的）。
  //
  // 这里在**加载时**就试打一次，把"运行到一半才炸"变成"挂载即失败"。
  if (typeof tool.output?.render !== "function") {
    throw new TypeError(
      "android_case: output.render 缺失或不是函数 —— " +
        "harness 的注册校验不会拦这个，但第一次调用必炸。");
  }
  try {
    const probe = tool.output.render(
      {}, { status: "PASS", engine: "model", report: "__selfcheck__" });
    // ★ 必须是 ContentBlock[]，不是字符串。
    //
    // 以前这里写的是 `probe !== "__selfcheck__"` —— 断言"render 要返回字符串"，
    // 而那正是 bug 本身。自检把错的形态钉死成了"正确"，所以：
    //   · 普通会话没问题（字符串照显示）
    //   · 测试模式（PTC）一跑就 content.some is not a function
    // 教训：自检断言的是"契约"，不能照着当前实现的样子写。
    if (!Array.isArray(probe)) {
      throw new Error(
        `render 必须返回 ContentBlock[]，实际是 ${typeof probe}：` +
          `${JSON.stringify(probe)}`);
    }
    if (!probe.some((b) => b && b.type === "text")) {
      throw new Error(`render 至少要有一个 text block：${JSON.stringify(probe)}`);
    }
    if (!probe.some((b) => typeof b.text === "string" &&
                          b.text.includes("__selfcheck__"))) {
      throw new Error(`render 没把 report 带出来：${JSON.stringify(probe)}`);
    }
  } catch (err) {
    throw new TypeError(`android_case: output.render 自检失败 -> ${err?.message ?? err}`);
  }

  ctx.tools.register(tool);
};

export { apply, inject, name };