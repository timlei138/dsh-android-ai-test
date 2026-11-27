# -*- coding: utf-8 -*-
"""`tools/common.py` 的「预期」切分（`split_case_expectations`）单测。

## 为什么必须测（N-31，真实数据）

`split_case_expectations` 的输出决定 **expect_id**：AI 记 finding 时按
「第几条预期」对齐（`findings.expect_id`），事后判 flaky 也靠它。
切错一条，那一轮的结论就挂到了错误的预期上 —— 而界面上看不出任何异常。

真实缺陷（`assets/cases/com.zui.calendar/com.zui.calendar_启动应用_03.md`）：
预期段用 `【16.0：` / `】` / `【16.1：…】` 标注**版本分支**，原来这些标记被
当成正文，于是切成 4 条：

    id=1 label=''   text='【16.0：'          ← 空壳，纯标记
    id=2 label='1'  text='日历弹框内容：…'
    id=3 label='2'  text='点击退出…】【16.1：1.日历CTA弹框内容：…'   ← 两版混一起
    id=4 label='2'  text='点击退出…】'

修复后应是干净的 4 条（16.0 的 1./2. + 16.1 的 1./2.），没有空壳、没有标记残留、
label 各归各位。**注意 id 是位置序号 1..N，原文编号只当 label 留存** ——
原文从 2 起、有重号（03 里两个 `2.`）都是常态，不是缺陷。

纯字符串处理，不需要设备。
"""
import io
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "tools"))

import common  # noqa: E402

CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def split(text: str) -> list[dict]:
    return common.split_case_expectations(text)


# 03 的预期段（逐字来自真实用例，只把长正文截短以免测试文件冗长 —— 结构不变）
CASE03 = (
    "测试用例 日历_03 启动应用-CTA权限弹窗（P2）\n"
    "\n"
    "预置条件：\n"
    "首次启动/清除应用数据，\n"
    "不连接网络\n"
    "\n"
    "测试步骤：\n"
    "1.首次进入日历-查看权限弹框\n"
    "2.点击同意、退出\n"
    "\n"
    "预期结果：\n"
    "【16.0：\n"
    "1.日历弹框内容：必要权限（用于基本功能和服务）\n"
    "2.点击退出退出应用，点击同意进入日历高级功能权限界面\n"
    "】\n"
    "【16.1：1.日历CTA弹框内容：\n"
    "日历需要使用以下权限\n"
    "2.点击退出，退出应用，点击同意即可获取对应权限】\n"
)


@case("★ 03：版本分支标记不能变成空壳预期（原来 id=1 是 '【16.0：'）")
def _():
    exps = split(CASE03)
    assert len(exps) == 4, [(e["id"], e["label"], e["text"][:20]) for e in exps]
    assert exps[0]["text"] != "【16.0：", exps[0]
    for e in exps:
        assert e["text"].strip(), f"空壳预期：{e}"


@case("★ 03：正文里不能残留【16.x / 单个】标记")
def _():
    exps = split(CASE03)
    for e in exps:
        assert "【16." not in e["text"], e["text"][:60]
        assert "】" not in e["text"], e["text"][:60]


@case("★ 03：`【16.1：1.日历CTA…` 要和它的 1. 分开（原来黏在 id=3 前面）")
def _():
    exps = split(CASE03)
    assert exps[2]["label"] == "1", exps[2]
    assert exps[2]["text"].startswith("日历CTA弹框内容"), exps[2]["text"][:40]


@case("03：label 保留原文编号（两个 2. 都原样留着，id 才是位置序号）")
def _():
    exps = split(CASE03)
    assert [e["id"] for e in exps] == [1, 2, 3, 4]
    assert [e["label"] for e in exps] == ["1", "2", "1", "2"]


@case("版本标记剥离：各种写法都要认（全角/半角/独立】行）")
def _():
    for raw in ("【16.0：", "【16.0:", "【16：", "【16.0.1：", "】", "  】  "):
        text = f"预期结果：\n{raw}\n1.第一条\n2.第二条\n"
        exps = split(text)
        assert len(exps) == 2, (raw, exps)
        for e in exps:
            assert "【" not in e["text"] and "】" not in e["text"], (raw, e)


@case("没有版本标记的普通用例：切分结果不受影响")
def _():
    text = (
        "预期结果：\n"
        "1.打开课程表页面\n"
        "2.列表里出现新课程\n"
        "·课程名称显示正确\n"
    )
    exps = split(text)
    assert len(exps) == 2, exps
    assert exps[0]["label"] == "1" and exps[0]["text"] == "打开课程表页面"
    # 续行（· 开头）要并进上一体
    assert "课程名称显示正确" in exps[1]["text"], exps[1]


@case("段头同行带第一条：预期结果 1. xxx")
def _():
    exps = split("预期结果 1.打开课程表页面\n2.列表出现新课程\n")
    assert len(exps) == 2, exps
    assert exps[0]["text"] == "打开课程表页面"


@case("遇到「操作步骤」段头就停，不把后面步骤当预期")
def _():
    text = "预期结果：\n1.第一条\n\n操作步骤：\n1.这不该是预期\n"
    exps = split(text)
    assert len(exps) == 1, exps
    assert "这不该是预期" not in exps[0]["text"]


@case("无「预期结果」段 → 空列表（调用方按「没有可对齐预期」处理）")
def _():
    assert split("") == []
    assert split("测试步骤：\n1.点点看\n") == []
    assert split(None) == []


@case("★ 全量回归：64 条真实用例里不得再出现空壳预期（纯版本标记）")
def _():
    case_dir = ROOT / "assets" / "cases" / "com.zui.calendar"
    if not case_dir.is_dir():
        print("     （跳过：用例目录不存在）")
        return
    shells = []
    total = 0
    for md in sorted(case_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8", newline="")
        for e in common.case_expectations(text):
            total += 1
            body = e["text"].strip()
            if not body or body in ("【", "】") or body.startswith("【16"):
                shells.append((md.name, e))
    assert total >= 150, f"用例总数异常：{total}"
    assert not shells, f"仍有空壳预期：{shells[:5]}"


def main() -> int:
    print("=" * 70)
    print("tools/common.py —— 用例「预期」切分")
    print("=" * 70)
    failed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as error:
            failed += 1
            print(f"  [FAIL] {name}  {error}")
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}  异常：{type(error).__name__}: {error}")
        else:
            print(f"  [PASS] {name}")
    print(f"\n  {len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
