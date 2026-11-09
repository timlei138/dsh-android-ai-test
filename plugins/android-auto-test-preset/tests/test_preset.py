# -*- coding: utf-8 -*-
"""验证 preset 定义：插件是否真实存在、必填 config 给了吗、引用对不对。

## 为什么这个测试重要

preset 的配置错误**不会"少个工具"，而是整个 preset 加载不出来**。
界面上表现为"Android 自动化测试"这个模式整个不见了 ——
很容易误判成文件没同步。

实测踩过两次：
  · tool-subagent 漏了 provider（schema 必填）
    -> tool-subagent: invalid config: $.provider missing required value
    -> 整个 android-tester 消失
  · tool-subagent 写了 model: inherit
    -> 源码是 `configured?.model ?? parentOptions.model`，省略即继承
    -> 写 "inherit" 会被当字面量模型名去路由，然后找不到

## 检查什么

  1. YAML 能解析
  2. 每个插件的 name 在 DSH 里真实存在
  3. 每个插件的 Config schema 里**必填**的字段都给了
  4. agentPreset / allowedAgentPresets 引用的 id 在本文件里存在
  5. 几个关键能力开关（executor 有 bash 无 subagent；双方 write/edit 关）
"""
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PRESET = ROOT / "preset" / "cordis.patch.yml"

# DSH 的插件安装目录
DSH = Path(
    r"C:\Users\leichao4\AppData\Local\Volta\tools\image\packages"
    r"\@deepseek-ai\dsh\node_modules\@deepseek-ai\dsh\node_modules"
    r"\@deepseek-ai")

ok = 0
bad = 0
fails: list[str] = []


def ck(name: str, cond: bool, extra: str = "") -> None:
    global ok, bad
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        bad += 1
        print(f"  [FAIL] {name}" + (f" -> {extra}" if extra else ""))
        fails.append(name)


print("=" * 74)
print("0) YAML 能解析吗")
print("=" * 74)
try:
    doc = yaml.safe_load(PRESET.read_text(encoding="utf-8"))
    ck("YAML 合法", True)
except Exception as exc:  # noqa: BLE001
    ck("YAML 合法", False, str(exc))
    print(f"\n  {bad} 处失败")
    sys.exit(1)

presets = {i["config"]["id"]: i["config"] for i in doc[0]["insert"]}
ck(f"定义了两个 preset：{list(presets)}", len(presets) >= 2)

print()
print("=" * 74)
print("1) 插件包在 DSH 里真实存在")
print("=" * 74)
names: set[str] = set()
for cfg in presets.values():
    for pl in cfg.get("plugins") or []:
        n = str(pl.get("name") or "")
        if n.startswith("@deepseek-ai/"):
            names.add(n)
        for sub in (pl.get("config") or []):
            if isinstance(sub, dict):
                sn = str(sub.get("name") or "")
                if sn.startswith("@deepseek-ai/"):
                    names.add(sn)

for n in sorted(names):
    short = n.replace("@deepseek-ai/", "")
    ck(f"{short}", (DSH / short).is_dir())

print()
print("=" * 74)
print("2) 必填 config 字段都给了")
print("=" * 74)
for n in sorted(names):
    short = n.replace("@deepseek-ai/", "")
    pkg = DSH / short
    js = None
    for cand in (pkg / "lib" / "index.js", pkg / "index.js"):
        if cand.is_file():
            js = cand
            break
    if not js:
        continue
    m = re.search(r"const Config = z\.object\(\{(.{0,3000}?)\}\)",
                  js.read_text(encoding="utf-8", errors="replace"), re.S)
    if not m:
        continue
    body = m.group(1)
    required = re.findall(r"(\w+):\s*z\.[^,]*?\.required\(\)", body)
    if not required:
        continue
    # 找这个插件在每个 preset 里给的 config
    for pid, cfg in presets.items():
        for pl in cfg.get("plugins") or []:
            if str(pl.get("id")) != short.replace("dsh-", ""):
                continue
            given = pl.get("config") or {}
            if not isinstance(given, dict):
                given = {}
            miss = [r for r in required if r not in given]
            ck(f"{short} / {pid} 必填齐全", not miss, f"缺 {miss}")

print()
print("=" * 74)
print("3) agentPreset 引用存在")
print("=" * 74)
for pid, cfg in presets.items():
    for pl in cfg.get("plugins") or []:
        c = pl.get("config")
        if not isinstance(c, dict):
            continue
        ap = c.get("agentPreset")
        if ap:
            ck(f"{pid}: agentPreset={ap} 存在", ap in presets)
        for a in (c.get("allowedAgentPresets") or []):
            ck(f"{pid}: allowed {a} 存在", a in presets)

print()
print("=" * 74)
print("4) 关键能力开关")
print("=" * 74)


def ids_of(pid: str) -> list[str]:
    return [str(p.get("id")) for p in presets[pid]["plugins"]]


def cfg_of(pid: str, plugin: str) -> dict:
    for p in presets[pid]["plugins"]:
        if p.get("id") == plugin:
            return p.get("config") or {}
    return {}


t = ids_of("android-tester")
e = ids_of("android-executor")

ck("tester 有 bash", "tool-bash" in t)
ck("tester 有 subagent", "tool-subagent" in t)
ck("tester write 关闭", cfg_of("android-tester", "tool-fs").get("write") is False)
ck("tester edit 关闭", cfg_of("android-tester", "tool-fs").get("edit") is False)
ck("executor 有 bash", "tool-bash" in e)
ck("executor 无 subagent", "tool-subagent" not in e)
ck("executor write 关闭",
   cfg_of("android-executor", "tool-fs").get("write") is False)
ck("executor edit 关闭",
   cfg_of("android-executor", "tool-fs").get("edit") is False)

print()
print("=" * 74)
if fails:
    print(f"  {bad} 处失败")
    for f in fails:
        print("   ✗", f)
else:
    print(f"  ✅ 全部通过（{ok} 项）")
print("=" * 74)
sys.exit(1 if bad else 0)
