# android-agent

在 Android 真机上按用例执行 GUI 测试。代码负责「能确定的部分」，
模型负责「不能确定的部分」。

## 目录结构

```
langgraph-poc/
├── run_case_json.py          ← 唯一入口：stdin 收 {case_name}，stdout 出报告 JSON
│
├── src/android_agent/        ← 全部业务代码
│   ├── cases/                用例层：读 YAML、算路径
│   │   ├── paths.py            资产/用例/知识卡目录定位
│   │   └── loader.py           YAML -> CaseSpec
│   ├── device/               设备层：唯一允许碰 adb 的地方
│   │   ├── bridge.py           工具层桥接（observe/act/session）
│   │   ├── lock.py             设备互斥锁
│   │   └── probe.py            手动调试探针
│   ├── agent/                Agent 层：LangGraph 状态机
│   │   ├── nodes.py            子 Agent 节点定义
│   │   ├── graph.py            用例级图
│   │   ├── runner.py           interrupt 循环驱动
│   │   ├── decide.py           决策通道（渲染 prompt、校验回复）
│   │   ├── vision.py           看图通道
│   │   └── child_runner.py     断点续跑
│   └── report/               报告层
│       ├── writeback.py        YAML 回写
│       └── scratch.py          tmp/ 管理与自动清理
│
├── assets/                   ← 运行时资产（独立 venv）
│   ├── tools/                  工具层：observe/act/checkpoint/pathmap...
│   ├── knowledge/              知识卡 + 路径图
│   └── storage/cases/          用例 YAML
│
├── scripts/                  ← 一次性工具脚本（审计、调试）
├── tests/                    ← 测试
├── tmp/                      ← 临时产物，超过 24h 自动清理
└── docs/                     ← 文档
```

## 分层规则

**`device/` 是唯一能调 adb 的层。** 别在别处 `subprocess.run(["adb", ...])` ——
绕过它就失去了会话锁、事件账本和崩溃检测。

**`cases/` 不碰设备。** 给它一个用例名，它只回答「有哪些步骤、判据是什么」。

**`agent/` 负责编排。** 节点是纯函数：输入 `ChildState`，输出要更新的字段。

**判定集中在 `nodes.py` 的 `evaluate_check` / `match_landing`。**
这是「能完全确定的由代码判、不确定的交 AI」这条分工的落点。

## 路径规范

统一从 `android_agent.cases.paths` 取，别自己拼 `Path(__file__).parent / "assets"`：

```python
from android_agent.cases import paths
paths.skill_root()    # 资产根目录
paths.cases_dir()     # 用例目录
paths.REPO_ROOT       # 仓库根
```

自己拼的路径在文件挪位置后会**静默指错**（不报错，只是找不到东西）。

## 临时文件

全部写 `tmp/`：

```python
from android_agent.report.scratch import tmp_dir
out = tmp_dir() / "report_7.json"
```

`run_case_json.py` 每次启动会清一次 `tmp/` 里超过 24 小时的文件。
手动清理：

```bash
python -m android_agent.report.scratch --dry-run    # 看会删什么
python -m android_agent.report.scratch              # 真删
```

## 跑用例

```bash
echo '{"case_name":"168"}' | .venv/Scripts/python.exe run_case_json.py
```

产物落在 `tmp/`：`report_168.json`、`report_168_subagent.json`、`run_168.log`。

## 跑测试

```bash
# Python
for f in tests/test_*.py; do .venv/Scripts/python.exe "$f"; done

# JS
cd tests && for f in test_*.mjs; do node "$f"; done
```

已知 4 个测试依赖已删除的用例文件（`test_case_yaml`、`test_goto_nav`、
`test_per_step_pitfalls`、`test_user_report_3`），用例重新生成后会恢复。

## 历史文档

早期 PoC 的设计记录在 `docs/`，其中 `开发记录-踩坑.md` 记录了
反复踩过的坑（尤其是「不要重复造工具层的轮子」），改动判定逻辑前值得一读。
