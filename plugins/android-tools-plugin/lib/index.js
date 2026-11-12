import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'

/**
 * Android 测试工具层的原生 tool 封装。
 *
 * ## 为什么要有这个插件
 *
 * 原来 AI 靠 shell 调用工具层脚本：
 *
 *     & "D:\...\python.exe" "D:\...\act.py" tap --x 100 --y 200 --via "rid=btn_ok"
 *
 * 在 Windows PowerShell 5.1 上有三个坑（实测 14 个会话全部踩到）：
 *
 *   ① 引号包裹的 exe 必须加 `&`，否则报 `Unexpected token`
 *   ② 中文长文本里的 `“ ”` 被当成字符串定界符，参数被截断
 *   ③ 子脚本 print 中文时按 GBK 编码，直接 UnicodeEncodeError 崩
 *
 * 每个坑都要多一轮试错。**根因是「把 shell 当 API 用」。**
 *
 * 注册成原生 tool 之后，参数以结构化 JSON 传入，不走任何命令行解析：
 *
 *     android_env()
 *     android_act({ action: "tap", x: 100, y: 200, via: "rid=btn_ok" })
 *
 * ## 实现方式
 *
 * 每个工具都是「用工具层的 venv 跑一个 Python 脚本，收回 stdout 的 JSON」。
 * 不重写业务逻辑 —— 工具层仍然是唯一真相，这里只换掉调用通道。
 *
 * shell 服务用法（`ctx.shell`）：
 *   1. `resolve(request)` -> spec   （补默认值、封顶）
 *   2. `execute(spec)` -> handle    （已经 spawn）
 *   3. `await handle.result()`      （等它跑完，拿 stdout/stderr/exitCode）
 * 只 `execute` 不 `await result()` 就是后台跑；这里全都要等结果。
 */

/** 插件名（Loader 用；同一 profile 内唯一）。 */
export const name = 'android-tools'

/**
 * 声明要用哪些服务。
 *
 * ⚠️ 缺这个会加载失败：
 *      android-tools: Error: cannot get property "tools" without inject
 *    Cordis 的规则是：**用到 ctx.xxx 就必须在 inject 里声明 xxx**，
 *    否则访问属性时直接抛错（不是返回 undefined）。
 */
export const inject = ['tools', 'shell']

/**
 * 配置 schema。
 *
 * ⚠️ 必须是 **schemastery 对象**（`z.object(...)`），不能是普通 JSON Schema。
 *    用普通对象会加载失败：
 *      TypeError: Cannot read properties of undefined (reading 'validate')
 *    Loader 会对它调 `.validate()`，那是 schemastery 的方法。
 */
export const Config = z.object({
  target: z
    .string()
    .description(
      '工具层根目录（含 .venv / tools / assets / storage），如 D:\\dsh\\android-ai-test',
    ),
  timeoutMs: z
    .number()
    .description('单次工具调用超时毫秒，默认 300000（5 分钟）')
    .default(300_000),
})

/** 把 stdout 里的 JSON 抠出来（脚本可能在前面打了日志行）。 */
function extractJson(text) {
  const trimmed = (text ?? '').trim()
  if (!trimmed) return null
  try {
    return JSON.parse(trimmed)
  } catch {
    // 不是整体 JSON，往下逐字符找起点
  }
  for (let i = 0; i < trimmed.length; i += 1) {
    if (trimmed[i] !== '{') continue
    try {
      return JSON.parse(trimmed.slice(i))
    } catch {
      // 继续试下一个 `{`
    }
  }
  return null
}

/** 把参数数组拼成一条命令行（每个参数单独加引号并转义内部引号）。 */
function toCommandLine(argv) {
  return argv
    .map((a) => {
      const s = String(a)
      if (s === '') return '""'
      if (!/[\s"&|<>^()]/.test(s)) return s
      return `"${s.replace(/"/g, '\\"')}"`
    })
    .join(' ')
}

/**
 * 按平台拼命令行。
 *
 * ★ 为什么不能只用上面那个：shell 服务在 POSIX 上把 command 交给 `sh -c` 执行。
 *   Windows 的引号规则套过来，`/a/b c/.venv/bin/python` 会变成
 *   字面量字符串 `"/a/b c/.venv/bin/python"` 而被 sh 当成「命令的第一个词」，
 *   报 `command not found`。POSIX 一律用单引号包 (单引号内无转义，最安全)。
 */
function buildCommandLine(argv) {
  if (process.platform === 'win32') return toCommandLine(argv)
  return argv
    .map((a) => {
      const s = String(a)
      if (s === '') return "''"
      if (!/[\s"'&|<>()$`\\!*?;~#^]/.test(s)) return s
      return `'${s.replace(/'/g, `'\\''`)}'`
    })
    .join(' ')
}

/** 各平台的 venv 解释器相对位置。 */
function venvPython(target) {
  if (process.platform === 'win32') return `${target}\\.venv\\Scripts\\python.exe`
  return `${target}/.venv/bin/python`
}

export function apply(ctx, config = {}) {
  const target = config.target
  const timeoutMs = Number(config.timeoutMs ?? 300_000)

  /**
   * 跑一个工具层脚本，返回解析后的 JSON。
   *
   * 解释器固定用 `<target>/.venv/Scripts/python.exe` —— 不用 PATH 里的
   * python（那上面没装 uiautomator2，会 ImportError）。
   *
   * ## 参数怎么传：stdin 传 JSON，不走命令行
   *
   * 2026-10-28 踩到的坑：`android_read_file(section='坑-…的两种"空"')`
   * 报 `usage: read_file.py`。查下来是参数里带**双引号**时：
   *
   *   toCommandLine 用 POSIX 规则把 " 转义成 \"，
   *   但 Windows cmd **不认 \" 这个转义**，把引号当结束符，
   *   于是 Python 收到的是 `"坑-…的两种\"` 被拆成两段 -> argparse 报错。
   *
   * 试过改成 `""` 双写，cmd 会把两个引号都当字面量传进去，也不对。
   *
   * 所以：**参数走 stdin 的 JSON，命令行只留脚本路径**。
   * 这样空格、引号、`&`、`|`、`%`、中文、emoji 全部安全 ——
   * 因为这些字符根本没进入命令行解析。
   *
   * 脚本侧约定（read_file.py 已实现）：
   *   命令行带 `--stdin-json` 时，从 stdin 读一个 JSON 对象当参数。
   */
  const runTool = async (script, args, exec) => {
    if (!target) {
      return {
        ok: false,
        error:
          'android-tools: config.target 未配置（应指向 android-ai-test 目录）',
      }
    }
    const py = venvPython(target)
    // 只有这两个路径进命令行 —— 它们由 config 和常量决定，不含用户输入
    const sep = process.platform === 'win32' ? '\\' : '/'
    const argv = [py, `${target}${sep}tools${sep}${script}`, '--stdin-json']
    const stdinPayload = JSON.stringify({ args })

    // 所有工具层脚本都要求 UTF-8 输出；显式给，别靠继承
    const request = {
      command: buildCommandLine(argv),
      workdir: target,
      timeoutMs,
      onExpiry: 'kill',
      env: {
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      },
      stdin: stdinPayload,
      signal: exec?.signal,
      // ★ 必须显式给 sandboxPolicy，否则 shell 服务按默认 workspace-write 处理，
      //   而本机的 Windows ACL sandbox backend 起不来，直接拒绝执行：
      //     sandbox mode "workspace-write" is requested but no sandbox backend
      //     is usable on this host ... Runner failure: windows-acl-run
      //
      //   测试工具要连 adb、读写 <target>\storage，本质上是「不受限」的操作；
      //   这里声明 danger-full-access 表示“我知道自己在跑什么，别拦”。
      //   当前会话的 DSH 文件策略也是 danger-full-access，两者一致。
      sandboxPolicy: {
        mode: 'danger-full-access',
        workspaceRoot: target,
      },
    }

    try {
      const spec = ctx.shell.resolve(request)
      const handle = await ctx.shell.execute(spec)
      const res = await handle.result()
      const out = res?.stdout?.text ?? ''
      const err = res?.stderr?.text ?? ''
      const parsed = extractJson(out)
      if (parsed) return parsed
      // 不是 JSON 也没关系 —— 有些脚本（env.py）输出人读文本。
      // 只要退出码 0 而且有输出，就当成功原样返回，别报成 error。
      if ((res?.exitCode ?? 0) === 0 && out.trim()) {
        return { ok: true, output: out }
      }
      return {
        ok: false,
        error: (err || out || '(no output)').slice(0, 2000),
        exit_code: res?.exitCode ?? null,
      }
    } catch (e) {
      return { ok: false, error: `执行失败: ${e?.message ?? e}` }
    }
  }

  ctx.tools.register(
    defineTool({
      name: 'android_env',
      description:
        'Android 测试工具层环境探测：返回 platform / shell / storage 路径、依赖是否装好（deps_ok）、以及设备 serial 与输入法是否就绪（device.ime_ok）。开工第一条就调它，确认产物落点和命令环境。',
      parameters: {},
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            platform: { type: 'string' },
            shell: { type: 'string' },
            storage_dir: { type: 'string' },
            deps_ok: { type: 'string' },
            device: { type: 'object', additionalProperties: true },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(_args, exec) {
        // --json 必须显式传：env.py 只有看到它才输出结构化 JSON，
        // 否则打人类可读文本，主 Agent 拿不到 device.ime_ok。
        return runTool('env.py', ['--json'], exec)
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'android_act',
      description:
        '在真机上执行一个动作（tap / longclick / input / swipe / back / home / key），执行后自动返回动作后的画面（截图 + UI 树 + 可点清单 next）。参数是结构化 JSON，不走 shell 解析。',
      parameters: {
        action: {
          type: 'string',
          required: true,
          enum: ['tap', 'longclick', 'input', 'swipe', 'back', 'home', 'key'],
          description: '动作类型',
        },
        x: { type: 'number', description: 'tap/longclick/input 的 x 坐标' },
        y: { type: 'number', description: 'tap/longclick/input 的 y 坐标' },
        via: {
          type: 'string',
          description:
            '落点依据，形如 rid=btn_ok / text=完成 / desc=关闭。会写进证据，事后可追',
        },
        why: { type: 'string', description: '为什么做这个动作（进时间轴）' },
        name: { type: 'string', description: '证据名，如 case176/step03' },
        expect: {
          type: 'string',
          description:
            '检查点：这一步应落在哪。形如 activity=XxxActivity 或 rid=xxx。只在已知落点时写，别凭记忆猜',
        },
        text: {
          type: 'string',
          description:
            'input 动作要输入的文字。传空字符串 "" = 清空输入框（默认覆盖式输入，不需要先删）',
        },
        from: { type: 'string', description: 'swipe 起点，形如 540,2000' },
        to: { type: 'string', description: 'swipe 终点，形如 540,800' },
        dur: { type: 'number', description: 'swipe 时长毫秒' },
        repeat: {
          type: 'number',
          description: '重复次数（仅 tap），用于滚轮连点',
        },
        gap: {
          type: 'number',
          description: 'repeat 时每次间隔毫秒，默认 120',
        },
        code: { type: 'string', description: 'key 动作的 keyevent 码' },
        append: {
          type: 'boolean',
          description: 'input 时追加到已有内容，而不是覆盖（默认覆盖）',
        },
        settle: {
          type: 'number',
          description:
            '动作后稳定等待毫秒数，默认 500。异步渲染页（联网解析等）可加大',
        },
        permAction: {
          type: 'string',
          enum: ['grant', 'deny'],
          description:
            '声明权限意图（仅测「拒绝」路径时用）：不写默认点同意。写 deny 后，点击触发的权限框会被点「不允许」',
        },
        perm: {
          type: 'string',
          description: '配合 permAction 的权限类型标记，如 media_images / camera',
        },
        serial: { type: 'string', description: '设备 serial' },
        full: {
          type: 'boolean',
          description: '返回全量 nodes（默认只给精简可点清单 next）',
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            ok: { type: 'boolean' },
            activity: { type: 'string' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = [String(args.action || '')]
        const push = (flag, value) => {
          if (value !== undefined && value !== null && value !== '') {
            rest.push(flag, String(value))
          }
        }
        push('--x', args.x)
        push('--y', args.y)
        push('--via', args.via)
        push('--why', args.why)
        push('--name', args.name)
        push('--expect', args.expect)
        // ★ text 不能走 push：空字符串是「清空输入框」的合法语义，
        //   而 push 会把空串当成「没传」丢掉。文本走 stdin 的 JSON argv，
        //   空串不会经过 cmd.exe 解析，原样到 argparse 手里。
        if (args.text !== undefined && args.text !== null) {
          rest.push('--text', String(args.text))
        }
        push('--from', args.from)
        push('--to', args.to)
        push('--dur', args.dur)
        push('--repeat', args.repeat)
        push('--gap', args.gap)
        push('--code', args.code)
        push('--settle', args.settle)
        push('--perm-action', args.permAction)
        push('--perm', args.perm)
        push('--serial', args.serial)
        if (args.append === true) rest.push('--append')
        if (args.full === true) rest.push('--full')
        return runTool('act.py', rest, exec)
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'android_observe',
      description:
        '看当前屏幕：返回前台包/activity、可点清单（next）、UI 树（nodes）。不需要设备动作时用它。',
      parameters: {
        serial: { type: 'string', description: '设备 serial' },
        name: { type: 'string', description: '证据名' },
        noFilter: {
          type: 'boolean',
          description:
            '不过滤系统界面节点（默认只保留当前前台包的节点）。要看状态栏/通知栏/系统弹窗时用',
        },
        full: { type: 'boolean', description: '返回全量 nodes' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            ok: { type: 'boolean' },
            activity: { type: 'string' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = []
        if (args.serial) rest.push('--serial', String(args.serial))
        if (args.name) rest.push('--name', String(args.name))
        if (args.noFilter === true) rest.push('--no-filter')
        if (args.full === true) rest.push('--full')
        return runTool('observe.py', rest, exec)
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'android_read_file',
      description:
        '读知识卡 / 路径卡 / 用例文件（UTF-8，永不乱码）。**强烈建议用 card 参数**：一次同时搜知识卡（坑与行为规律）和路径卡（activity 与认出条件）。也可以用 path 读单个文件：默认给章节目录，query 做自然语言检索，section 按章节读，grep 精确匹配，offset/limit 按行读。',
      parameters: {
        card: {
          type: 'string',
          description:
            '应用包名，如 com.zui.calendar。**推荐用法**：一次同时搜 assets/knowledge/<包名>.md（知识卡）和 assets/knowledge/paths/<包名>.md（路径卡），结果按来源标注。用这个就不用记两条路径',
        },
        path: {
          type: 'string',
          description:
            '单个文件路径（给了 card 就不用它）。相对路径按工具层根目录解析，如 assets/knowledge/com.zui.calendar.md',
        },
        query: {
          type: 'string',
          description:
            '自然语言问题，如「怎么在课程表里加一节课」。自动做分词 + 同义词扩展 + 标题加权；匹配不到会返回完整目录',
        },
        section: {
          type: 'array',
          items: { type: 'string' },
          description:
            '章节名（可以是部分匹配），只读这几节，如 ["课程表空状态", "新建日程"]。**数组，一次可传多节**；只读一节也传数组',
        },
        grep: {
          type: 'string',
          description: '精确子串匹配（找 rid / 文案），返回命中行及上下文',
        },
        offset: { type: 'number', description: '从第几行开始（1-based）' },
        limit: { type: 'number', description: '读几行，默认 120' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            ok: { type: 'boolean' },
            mode: { type: 'string' },
            content: { type: 'string' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = []
        const push = (flag, value) => {
          if (value !== undefined && value !== null && value !== '') {
            rest.push(flag, String(value))
          }
        }
        push('--card', args.card)
        push('--path', args.path)
        push('--query', args.query)
        // section 是数组：argparse 侧 --section 是 action="append"，所以要重复传
        if (Array.isArray(args.section)) {
          for (const s of args.section) {
            if (s !== undefined && s !== null && s !== '') {
              rest.push('--section', String(s))
            }
          }
        } else if (args.section) {
          rest.push('--section', String(args.section))
        }
        push('--grep', args.grep)
        push('--offset', args.offset)
        push('--limit', args.limit)
        return runTool('read_file.py', rest, exec)
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'android_read',
      description:
        '对当前屏幕截图做 OCR，返回文字及其坐标（center 可直接当作 android_act 的 x/y）。用于读 toast、Canvas 自绘控件这类 UI 树拿不到的文字。',
      parameters: {
        serial: { type: 'string', description: '设备 serial' },
        name: { type: 'string', description: '证据名' },
        image: {
          type: 'string',
          description: '指定已存在的截图路径（不给则现截一张）',
        },
        region: {
          type: 'string',
          description: '只保留某矩形内的文字，形如 x1,y1,x2,y2',
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            ok: { type: 'boolean' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = []
        if (args.serial) rest.push('--serial', String(args.serial))
        if (args.name) rest.push('--name', String(args.name))
        if (args.image) rest.push('--image', String(args.image))
        if (args.region) rest.push('--region', String(args.region))
        return runTool('read.py', rest, exec)
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'android_vision',
      description:
        '问视觉模型看图（OpenAI 兼容通道）。**用于 OCR 认不出来的东西**：图标（加号/删除/对勾）、颜色、置灰、布局、控件朝向。问「上面写了什么字」请用 android_read（OCR 更快更准）；问「有没有加号/是不是红色」必须用这个 —— **OCR 只认文字，认不了图标**（实测：一张明明有加号的截图，OCR 输出 40 行文字一个 + 都没有）。',
      parameters: {
        image: {
          type: 'string',
          required: true,
          description:
            '图片路径。相对路径按工具层根目录解析，如 storage/evidence/case176/step03/shot.png',
        },
        prompt: {
          type: 'string',
          required: true,
          description:
            '问题。**要具体、可判**，如「第一个小节格子里有没有加号图标？答有/没有」「这个按钮是不是灰色不可点？」',
        },
        max_tokens: {
          type: 'number',
          description: '回答长度上限，默认 500',
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            ok: { type: 'boolean' },
            text: { type: 'string' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = [
          '--image', String(args.image || ''),
          '--prompt', String(args.prompt || ''),
        ]
        if (args.max_tokens) {
          rest.push('--max-tokens', String(args.max_tokens))
        }
        return runTool('vision.py', rest, exec)
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'android_perm',
      description:
        '查 / 授 / 撤 App 的运行时权限（走 adb，不经过界面弹窗）。**只在必要时用**：用例明确要测某个需要权限的功能、而权限还没授予时，先查一次再决定；或需要复现「权限没给」的场景时撤销。日常点击触发的权限弹窗由 android_act 自动处理，不用管。',
      parameters: {
        package: {
          type: 'string',
          required: true,
          description: '被测 App 的包名，如 com.zui.calendar',
        },
        grant: {
          type: 'string',
          description:
            '要授予的权限，逗号分隔。短名就行，如 "READ_CALENDAR,POST_NOTIFICATIONS"，会自动补 android.permission. 前缀',
        },
        revoke: {
          type: 'string',
          description:
            '要撤销的权限，逗号分隔（测「拒绝路径」时用）。短名同样会自动补前缀',
        },
        serial: { type: 'string', description: '设备 serial；只有一台设备时可省略' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            ok: { type: 'boolean' },
            runtime_permissions: { type: 'array' },
            granted_count: { type: 'number' },
            total_count: { type: 'number' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = ['--package', String(args.package || ''), '--json']
        if (args.grant) rest.push('--grant', String(args.grant))
        if (args.revoke) rest.push('--revoke', String(args.revoke))
        if (args.serial) rest.push('--serial', String(args.serial))
        return runTool('perm_tool.py', rest, exec)
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'android_state',
      description:
        '设备状态原语，不动屏幕内容：clear（清空应用数据，回到首次使用态）/ start / stop / grant / revoke。**只在用例明确要求时才用**：用例说「首次使用」「清理数据」才 clear；测「已授权」「拒绝」路径时用 grant/revoke 预置状态，弹窗根本不出现。日常权限弹窗由 android_act 自动处理。每次调用都自动记入会话事件流。',
      parameters: {
        action: {
          type: 'string',
          required: true,
          enum: ['clear', 'start', 'stop', 'grant', 'revoke'],
          description: '要做的状态操作',
        },
        package: {
          type: 'string',
          required: true,
          description: '被测 App 的包名，如 com.zui.calendar',
        },
        perm: {
          type: 'string',
          description:
            'grant/revoke 用。短名（camera/location/coarse_location/mic/contacts/storage/media_images/notifications）或 android.permission.XXX 全名',
        },
        activity: {
          type: 'string',
          description: 'start 用，可选的相对 activity 名，如 .MainActivity',
        },
        serial: { type: 'string', description: '设备 serial' },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            ok: { type: 'boolean' },
            hint: { type: 'string' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = [String(args.action || '')]
        if (args.package) rest.push('--package', String(args.package))
        if (args.perm) rest.push('--perm', String(args.perm))
        if (args.activity) rest.push('--activity', String(args.activity))
        if (args.serial) rest.push('--serial', String(args.serial))
        return runTool('state.py', rest, exec)
      },
    }),
  )

  ctx.tools.register(
    defineTool({
      name: 'android_system',
      description:
        '系统级界面动作（通知栏 / 快捷设置 / 亮屏解锁 / 多任务）。**别再用 swipe 手势下拉通知栏**：手势要求起点贴顶、速度有讲究、坐标随分辨率变，历史上同一会话里反复"再试一次拉下通知栏"的有 9 个；系统调用本身只要 43 毫秒。一次调用内做完「打开 → 截图 → dump → 解析出通知列表」，所以比「act 下拉 + observe 看」少一次往返（约省 5 秒）。返回值里的 notifications 已按条解析好（title/texts/clickable/center），判「提醒有没有来」直接看它，不必再 observe。',
      parameters: {
        action: {
          type: 'string',
          required: true,
          enum: [
            'notifications',
            'quick_settings',
            'close_shade',
            'wake',
            'unlock',
            'recents',
          ],
          description:
            'notifications=展开通知栏并解析通知列表 / quick_settings=展开快捷设置 / close_shade=收起面板 / wake=点亮屏幕 / unlock=点亮并上滑解锁（无密码锁屏）/ recents=打开多任务',
        },
        close: {
          type: 'boolean',
          description:
            'notifications / quick_settings 用：证据拍完顺手收起。证据仍是「展开的样子」，收起后 next 为空（那些坐标已失效）',
        },
        why: { type: 'string', description: '为什么做这个动作（进时间轴）' },
        name: { type: 'string', description: '证据名，如 case85/step07' },
        expect: {
          type: 'string',
          description:
            '检查点：这一步应落在哪。形如 activity=XxxActivity 或 rid=xxx',
        },
        settle: {
          type: 'number',
          description: '动作后稳定等待毫秒数，默认 500',
        },
        serial: { type: 'string', description: '设备 serial；只有一台设备时可省略' },
        full: {
          type: 'boolean',
          description: '返回全量 nodes（默认只给精简可点清单 next）',
        },
      },
      output: {
        schema: {
          type: 'object',
          additionalProperties: true,
          properties: {
            ok: { type: 'boolean' },
            shade_open: { type: 'boolean' },
            notifications: { type: 'array' },
            notification_count: { type: 'number' },
            hint: { type: 'string' },
          },
        },
        render: (_a, v) => [
          { type: 'text', text: typeof v === 'string' ? v : JSON.stringify(v) },
        ],
      },
      async execute(args, exec) {
        const rest = [String(args.action || '')]
        const push = (flag, value) => {
          if (value !== undefined && value !== null && value !== '') {
            rest.push(flag, String(value))
          }
        }
        if (args.close === true) rest.push('--close')
        push('--why', args.why)
        push('--name', args.name)
        push('--expect', args.expect)
        push('--settle', args.settle)
        push('--serial', args.serial)
        if (args.full === true) rest.push('--full')
        return runTool('system.py', rest, exec)
      },
    }),
  )
}
