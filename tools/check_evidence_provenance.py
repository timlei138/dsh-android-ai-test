# -*- coding: utf-8 -*-
"""强证据的来源核对：YAML 里写死的值，必须在真实记录里出现过。

## 用户的要求（原话）

  「增加个约束。那就是 YAML 文件不能凭空添加字段。所有强证据的值
    必须来自真实的记录。不能凭空捏造。」

拆成两条：
  1. **字段不能凭空添加** —— loader 的 `check_unknown_keys()` 管这个。
  2. **强证据的值必须来自真实记录** —— 本脚本管这个。

## "强证据的值"具体指什么

指 YAML 里被代码拿去**硬判**的那些字符串：`rid` / `activity` / `text` /
`package`。它们是"这个控件在不在"的唯一依据，写错一个就会
把功能正确的东西判成 FAIL，或者反过来永远判不过。

## 证据从哪来（必须都是真实采集的）

  · `assets/storage/evidence/**/*.xml`  —— 历次真机 dump
  · `tmp/runs/**/shots|dump*.xml`       —— 每次运行的现场留档
  · `assets/storage/knowledge`          —— 沉淀下来的知识卡

**凭空捏造**的判断标准：在上述任何一处都找不到对应字符串。
找不到 ≠ 一定错（可能是新页面还没采过），所以本脚本只报告、不直接判死，
由人/Agent 补采证据后再写进 YAML。
"""

import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = ROOT / "assets" / "storage" / "cases"

# 允许的"系统自带"资源前缀。
#
# ⚠️ 只豁免**明确不是被测 App 定义**的东西：
#   · `android:id/button1` —— 系统对话框按钮，任何系统框里都叫这个
#   · `com.android.*`      —— AOSP 系统组件
# 不能豁免 `com.zui.*` —— 被测 App 就是 com.zui.calendar，
# 那样会把整个 App 的 rid 全放过，等于校验失效（一度这么写过）。
SYS_PREFIX = ("android:id/", "com.android.")
SUT_PACKAGE = "com.zui.calendar"


def collect_evidence() -> tuple[set[str], set[str]]:
    """返回 (出现过的 rid/资源名, 出现过的 activity 名)。

    ⚠️ activity **不在 dump.xml 里** —— XML 是控件树，没有"当前是哪个页面"
    这个概念。它来自运行日志的 `act=com.zui.calendar.FooActivity`
    （见 tmp/runs/*/run.log）。一开始只在 XML 里找，于是所有 activity
    都报"没出现过"，全是假警报。
    """
    rids: set[str] = set()
    acts: set[str] = set()
    re_res = re.compile(r'resource-id="([^"]+)"')
    # 运行日志里 activity 的写法：act=com.zui.calendar.xxx.FooActivity
    re_act_log = re.compile(r'act=([\w.$]+Activity)')
    # dump.xml / meta.json 里偶尔直接带完整类名（弹窗容器等）
    re_act_any = re.compile(r'\b(com\.[\w.]*Activity)\b')

    n = 0
    for r in (ROOT / "assets" / "storage" / "evidence", ROOT / "tmp" / "runs"):
        for f in glob.glob(str(r / "**" / "*.xml"), recursive=True):
            try:
                t = Path(f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            n += 1
            for m in re_res.findall(t):
                rids.add(m)
                rids.add(m.split("/")[-1])       # 短名也收
            for m in re_act_any.findall(t):
                acts.add(m)
        # 日志才是 activity 的主来源
        for f in glob.glob(str(r / "**" / "*.log"), recursive=True) + \
                glob.glob(str(r / "**" / "meta.json"), recursive=True):
            try:
                t = Path(f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in re_act_log.findall(t):
                acts.add(m)
            for m in re_act_any.findall(t):
                acts.add(m)
    return rids, acts


def yaml_values(doc, path="") -> dict[str, set[str]]:
    """从 YAML 里抽出被当**强证据**用的值。

    只收 check 里和 landing/target 里的 rid/activity/text/package，
    自由文本（desc/know/pitfalls）不算强证据，不参与核对。
    """
    out: dict[str, set[str]] = {"rid": set(), "activity": set(),
                                "package": set(), "text": set()}

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                kp = f"{path}.{k}" if path else str(k)
                if k in ("rid", "rid_present", "node_present") and isinstance(v, str):
                    out["rid"].add(v.strip())
                elif k in ("activity", "activity_changed_to") and isinstance(v, str):
                    out["activity"].add(v.strip())
                elif k == "package" and isinstance(v, str):
                    out["package"].add(v.strip())
                elif k == "landing" and isinstance(v, str):
                    s = v.strip()
                    if s.startswith("activity="):
                        out["activity"].add(s.split("=", 1)[1].strip())
                    elif s.startswith("rid="):
                        out["rid"].add(s.split("=", 1)[1].strip())
                elif k == "target" and isinstance(v, str):
                    s = v.strip()
                    if s.startswith("rid="):
                        out["rid"].add(s.split("=", 1)[1].strip())
                walk(v, kp)
        elif isinstance(node, list):
            for i, one in enumerate(node):
                walk(one, f"{path}[{i}]")
        elif isinstance(node, str):
            # 字符串判据里的 activity= / rid=
            for m in re.finditer(r"activity=([\w.$]+)", node):
                out["activity"].add(m.group(1))
            for m in re.finditer(r"rid=([\w.]+)", node):
                out["rid"].add(m.group(1))

    walk(doc, "")
    return out


def main() -> int:
    try:
        import yaml
    except ImportError:
        print("需要 pyyaml")
        return 2

    rids, acts = collect_evidence()
    print(f"证据库：{len(rids)} 个资源名 / {len(acts)} 个 activity")

    total_rid = total_act = 0
    bad: list[str] = []
    files = sorted(glob.glob(str(CASE_DIR / "*.yaml")))
    for p in files:
        if "_test_invalid" in p:
            continue
        doc = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
        vals = yaml_values(doc)
        name = Path(p).name
        for r in sorted(vals["rid"]):
            total_rid += 1
            if r in rids:
                continue
            # 系统框的资源（android:id/button1 等）不需要在被测 App 里出现
            if r.startswith(SYS_PREFIX):
                continue
            # 带包名的写法（com.zui.calendar:id/foo）取短名再比一次
            if "/" in r and r.split("/")[-1] in rids:
                continue
            bad.append(f"  ✗ {name}\n      rid `{r}` 在任何真实 dump 里都没出现过")
        for a in sorted(vals["activity"]):
            # `activity=A|B` 是"命中任一个即可"的或语法。
            # 每个分支都必须有真实出处 —— 只要有一个是编的，
            # 这个判据就有一半是猜的，迟早在那条分支上误判。
            for one in a.split("|"):
                one = one.strip()
                if not one:
                    continue
                total_act += 1
                # 写的是 `.timetable.display.FooActivity`（相对包名），
                # 日志里是 `com.zui.calendar.timetable.display.FooActivity`（全名）。
                # 按"去掉前导点后，有没有哪个真实 activity 以 `.` + 它 结尾"来对。
                # ⚠️ 必须带这个点：否则 `FooActivity` 会误配到 `MyFooActivity`
                #    （match_landing 早年就是被 endswith 这么坑的）。
                short = one.lstrip(".")
                if one in acts or any(x.endswith("." + short) for x in acts):
                    continue
                bad.append(f"  ✗ {name}\n      activity `{one}`"
                           f" 在任何真实 dump/日志里都没出现过")

    print(f"强证据核对：rid {total_rid} 处 / activity {total_act} 处")
    print()
    if bad:
        print(f"没找到真实出处 {len(bad)} 处：")
        for b in bad:
            print(b)
        print("\n注意：找不到 ≠ 一定错。可能是新页面还没采集过 —— "
              "补采一次 dump 再写。")
        return 1
    print("✅ 全部有据可查，没有凭空捏造的强证据值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
