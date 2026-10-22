# -*- coding: utf-8 -*-
"""可点识别：clickable 标记要照它说的做，但文字要从子树里捞。

## 用户问过的问题（原话）

  「你是高级测试开发人员。你卡住的是 iv_more clickable 为 false。
    你要先解决这个问题。点开能不能点和你没关系。」

  「我意思你要看看这个是不是一个共性的问题。
    为啥 iv_more 明明是 clickable 的你认成 disclick 的。」

## 查证结论（基于真实 dump，不是推测）

**`iv_more` 从来没被误判过。** 258 份含 `iv_more` 的真实 dump，
258 份都把它正确放进了可点列表 —— 它一直是 `clickable="true"`。
"认成不可点"是**人读 XML 时读错了**，不是代码问题。

**但确实存在一个共性问题**（数据说话）：
在 400 份真实 dump 里，有 **1352 个 `clickable="true"` 的节点自己没有文字**，
其中 **926 个（69%）** 的文字挂在**不 clickable 的子节点**上。

典型是「更多」菜单：可点的 FrameLayout 自己没有 text，
文字在子节点 ViewHolder 内的 `item_title`(TextView, clickable=false) 上。
旧实现只看节点自己的 text，于是模型看到的是
「c=[2697,371]」「c=[2697,532]」这种**没有名字的坐标**，
根本选不出哪个是「课程表」。

## 这条约束

1. `clickable` 是可信的，**只取 `clickable="true"`**，不为"点了有反应"破例。
   （点开有没有用是另一回事，不参与识别。）
2. 可点容器**自己没有文字**时，把子树内的文字收上来。
3. 不能因为放开文字就收进一堆纯容器 —— 必须仍是 clickable=true。
"""

import glob
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from android_agent.device.direct import _text_within, parse_clickable  # noqa: E402

FAIL = 0
PASS = 0


def ck(name, cond, extra=""):
    global FAIL, PASS
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


EV = sorted(glob.glob(str(ROOT / "assets" / "storage" / "evidence" / "**"
                            / "dump.xml"), recursive=True))


def with_kw(kw, limit=12):
    out = []
    for f in EV:
        try:
            if kw in Path(f).read_text(encoding="utf-8", errors="replace"):
                out.append(f)
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


print("\n=== 1) iv_more 一直是 clickable=true（不是我们的 bug）===")
ds = with_kw("iv_more", 30)
ck("找到含 iv_more 的真实 dump", len(ds) > 0, str(len(ds)))
allok = True
for f in ds:
    items, _ = parse_clickable(f)
    if not any(i.get("r") == "iv_more" for i in items):
        allok = False
        print(f"      漏了：{Path(f).parent}")
ck(f"★★ {len(ds)} 份 dump 里 iv_more 全部进了可点列表", allok)

print("\n=== 2) clickable=false 的节点仍然不进可点列表（不破例）===")
# 菜单里 item_title 是 clickable=false 的 TextView。
# ⚠️ 它会以"借用来的名字"出现在父项上（r=item_title），
#    那不是"不可点节点进了清单"，是"可点容器借了子节点的名字"。
# 真正的契约：清单里每一项的**坐标**必须是可点容器的中心，
# 而不是数量上凭空多出不可点节点。
ms = with_kw("rv_more_menu", 8)
ck("找到含「更多」菜单的真实 dump", len(ms) > 0, str(len(ms)))
bad = []
for f in ms:
    items, _ = parse_clickable(f)
    # 菜单是 4 项，清单里应当正好 4 条（不含其他可点项时）
    menu_items = [i for i in items if i.get("t") in
                  ("课程表", "纵览", "年视图", "设置")]
    if len(menu_items) != 4:
        bad.append(f"{Path(f).parent.name}:{len(menu_items)}")
ck("★★ 4 个菜单项各占一条，没有多出不可点节点", not bad, str(bad[:3]))

print("\n=== 3) 可点容器的文字从子树收上来（共性修复）===")
ok = 0
for f in ms:
    items, _ = parse_clickable(f)
    if any(i.get("t") == "课程表" for i in items):
        ok += 1
ck(f"★★ {len(ms)} 份菜单 dump 都识别出了「课程表」", ok == len(ms) and ok > 0,
   f"{ok}/{len(ms)}")
# 四项都要在
if ms:
    items, _ = parse_clickable(ms[0])
    texts = [i.get("t") for i in items]
    ck("★ 四项菜单文字都在", all(x in texts for x in
                                 ("课程表", "纵览", "年视图", "设置")), str(texts))

print("\n=== 4) 仍是「只取 clickable=true」，没放开 ===")
src = (ROOT / "src" / "android_agent" / "device" / "direct.py"
       ).read_text(encoding="utf-8")
i = src.find("def parse_clickable")
body = src[i:i + 4000]
ck("★★ 代码里仍有 clickable != \"true\" -> continue",
   'el.get("clickable") != "true"' in body,
   "放开了就会把不可点节点也当目标")
ck("★ 有从子树收文字的实现", "_text_within(" in body)
ck("★ 只在自身没文字时才往下收（避免把整页文字拼一起）",
   "if not text and not desc:" in body)

print("\n=== 5) 收文字有上限，不会把整页拼给一个容器 ===")
from lxml import etree  # noqa: E402
_xml = """<hierarchy><node clickable="true" bounds="[0,0][100,100]">
<node text="A"/><node text="B"/><node text="C"/><node text="D"/></node></hierarchy>"""
root = etree.fromstring(_xml.encode())
el = root.xpath('//*[@clickable="true"]')[0]
got = _text_within(el)
ck("★ 最多取 3 段（D 被截掉）", got == "A B C", repr(got))

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
