# -*- coding: utf-8 -*-
"""`tools/read_file.py` 读知识卡的**输出结构**单测。

## 为什么测这个而不是测内容

技能卡（`SKILL.md`）对 AI 承诺的是「每个 match 的 `body` 是全文」「`--section`
拿到的就是那一节」这类**结构契约**。契约错了 AI 不是报错，是拿到 None 之后
自己编 —— 这类缺陷从来不会自己冒出来。历史上就是这样漏的：实测返回里根本
没有 `body` 字段（全文在 `content`），传一个 `--section` 与传两个还是两套
顶层 schema。

这里只断言**形状**（有哪些键、外层结构一不一致、失败时 rc 是不是 0），
不断言具体文案 —— 文案会随知识卡修订变，形状不该变。
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

import read_file as rf  # noqa: E402

PKG = "com.zui.calendar"
CASES = []


def case(name):
    def decorate(fn):
        CASES.append((name, fn))
        return fn
    return decorate


def _card(**kwargs):
    pkg = kwargs.pop("pkg", PKG)
    query = kwargs.pop("query", None)
    section = kwargs.pop("section", None)
    grep = kwargs.pop("grep", None)
    return rf._card_mode(pkg, query, section, grep)


def _cli(argv: list[str]) -> dict:
    """跑一次真实的 `read_file.py`，把 stdout 当 JSON 解，并把 rc 塞进去。"""
    import json
    import subprocess
    proc = subprocess.run([sys.executable, str(ROOT / "tools" / "read_file.py")] + argv,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(ROOT))
    text = (proc.stdout or "").strip()
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {"raw": text[:400]}
    payload["rc"] = proc.returncode
    return payload


@case("夹具：被测应用的知识卡存在")
def _():
    paths = rf._card_paths(PKG)
    assert paths, f"找不到 {PKG} 的知识卡"


@case("query 模式：每个 match 都带 body（技能卡承诺的字段名）")
def _():
    out = _card(query="课程表")
    assert out.get("ok"), out
    cards = out.get("cards") or []
    assert cards, out
    matched = [m for c in cards for m in (c.get("matches") or [])]
    assert matched, "query 没匹配到任何小节"
    for m in matched:
        assert "body" in m, f"match 缺 body 字段：{sorted(m)}"
        assert isinstance(m["body"], str) and m["body"].strip()


@case("section 单节：走 _flatten_card 后顶层有 content/found，且 cards 保留")
def _():
    out = _card(section="课程表")
    assert out.get("ok"), out
    flat = rf._flatten_card(out)
    assert "content" in flat and isinstance(flat["content"], str)
    assert "found" in flat and isinstance(flat["found"], list)
    assert flat["found"], "found 为空，AI 拿不到「命中了哪一节」"
    for f in flat["found"]:
        assert "source" in f and "title" in f, f


@case("section 单节与多节（走真实 CLI）：顶层 schema 一致，rc 都是 0")
def _():
    # 单节/多节的分叉在 `_main()` 里，不在 `_card_mode` 里 —— 所以要起 CLI 才测得准。
    one = _cli(["--card", PKG, "--section", "课程表"])
    two = _cli(["--card", PKG, "--section", "课程表", "--section", "空状态"])
    assert one["rc"] == 0, one
    assert two["rc"] == 0, two
    for payload, tag in ((one, "单节"), (two, "多节")):
        assert payload.get("ok"), f"{tag}未 ok：{payload}"
        for key in ("content", "found"):
            assert key in payload, f"{tag}缺 {key}：{sorted(payload)}"
        assert isinstance(payload["content"], str)
        assert isinstance(payload["found"], list)
    assert two.get("sections_asked"), "多节应回显问的是哪几节"


@case("无卡包时 CLI 用非 0 退出码（AI 才能察觉「这卡不存在」）")
def _():
    got = _cli(["--card", "com.no.such.package.xyz"])
    assert got["rc"] != 0, f"rc 仍是 0：{got}"


@case("无参调用：toc 模式的标题能通过 found 拿到（顶层 toc 被拍平也不丢）")
def _():
    out = _card()
    if not out.get("ok"):
        return
    flat = rf._flatten_card(out)
    titles = [f.get("title") for f in flat.get("found") or []]
    assert titles, "目录模式拍平后 found 空，标题全丢了"


@case("失败要能看出来：ok=false 的返回里带 error")
def _():
    out = _card(pkg="com.no.such.package.xyz")
    assert out.get("ok") is False, out
    assert out.get("error") or out.get("hint"), out


@case("section 不存在时给 fuzzy 提示，不是静默空内容")
def _():
    out = _card(section="这个节名一定不存在xyz")
    if not out.get("ok"):
        return
    flat = rf._flatten_card(out)
    # 要么明说没找到，要么给出模糊命中的候选 —— 不能 content 空空还 ok
    has_signal = bool(flat.get("fuzzy_sections")) or "未" in flat.get("content", "") \
        or not flat.get("found")
    assert has_signal, flat.get("content", "")[:200]


def main() -> int:
    failed = 0
    for name, fn in CASES:
        try:
            fn()
            print(f"  [PASS] {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  [FAIL] {name} -> {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name} -> {type(exc).__name__}: {exc}")
    print(f"\n  {len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
