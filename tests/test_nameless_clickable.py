# -*- coding: utf-8 -*-
"""无名可点容器：既要能认出来，也不能因为重名就放行。

## 背景（176 实测，卡到 80 轮上限）

空课程表的 25 个格子，每一个都是：
    FrameLayout  clickable=true  没有 resource-id  没有 text
        └─ ImageView  iv_add_hint   clickable=false   <-- 加号图标

旧实现在可点清单里把它们输出成 `{'c': [552, 648]}` —— **没有名字**。
模型看到 20 多个一模一样的无名坐标，说不出"点第一个小节"，
于是反复点同一个位置，一直点到 80 轮上限。

## 两条约束（缺一不可）

1. **给名字**：自己没有 rid、没有文字时，借用子节点的 rid 当识别名。
   否则模型永远无法引用这个格子。
2. **重名不许糊弄**：借来的名字会让 25 个格子全叫 cv_empty_content。
   所以 `rid=xxx` 命中多个时**仍然拒绝**，必须用 `rid=xxx#序号` 明确指定第几个。
   绝不能退化成"取第一个"—— 那就是"两个相同的找错了一个"。

⚠️ 这是**识别名**，不改变 clickable 的判断：
   加号图标 iv_add_hint 自己是 clickable=false，永远不进可点清单。
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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


from android_agent.agent.nodes import _resolve_strong_target  # noqa: E402
from android_agent.device.direct import parse_clickable  # noqa: E402

# 造一份和真机结构一致的 XML：25 个无名可点格子 + 中间一个不可点的加号
_GRID = "".join(
    f'<node class="android.widget.FrameLayout" clickable="true" '
    f'resource-id="" text="" bounds="[{276 + (i % 5) * 552},'
    f'{483 + (i // 5) * 330}][{828 + (i % 5) * 552},'
    f'{813 + (i // 5) * 330}]">'
    f'<node class="android.widget.ImageView" clickable="false" '
    f'resource-id="com.zui.calendar:id/iv_add_hint" text="" '
    f'bounds="[486,582][618,714]"/></node>'
    for i in range(25)
)
_XML = ('<hierarchy rotation="0"><node class="android.widget.FrameLayout">'
        + _GRID + "</node></hierarchy>")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "grid.xml"
    p.write_text(_XML, encoding="utf-8")
    items, _ = parse_clickable(p)

    print("\n=== 1) 无名可点格子必须有个名字（模型才能引用）===")
    ck("★★ 25 个格子都在可点清单里", len(items) == 25, str(len(items)))
    ck("★★ 每个格子都借到了名字 cv_empty_content 来源的 rid",
       all(i.get("r") for i in items),
       f"没名字的：{[i for i in items if not i.get('r')][:2]}")
    ck("★ 名字来自子节点（iv_add_hint 那一系）",
       all("iv_add_hint" in str(i.get("r")) or "cv_empty" in str(i.get("r"))
           for i in items), str(items[0]))

    print("\n=== 2) 加号图标只借名字，不产生多余的目标 ===")
    # 格子借用子节点的 rid 当名字，所以 r=iv_add_hint 会出现在**格子**上。
    # 那不是"图标进了可点清单"，是"格子叫这个名字"。
    #
    # ⚠️ 不能拿"坐标是否等于图标中心"来判：加号画在格子正中，
    #    格子中心本来就 = 图标中心（552,648）。那样写测试是错的。
    # 真正的契约是**数量**：25 个格子就该是 25 个目标，图标不额外加一条。
    ck("★★ 目标数量等于格子数量（图标没额外多出一条）",
       len(items) == 25, f"{len(items)} 个")
    ck("★ 25 个坐标互不相同（每个格子各占一个）",
       len({tuple(i["c"]) for i in items}) == 25,
       f"去重后 {len({tuple(i['c']) for i in items})} 个")

    st = {"obs_clickable": items, "obs_xml_path": str(p),
          "width": 3040, "height": 1904, "last_activity": ""}

    print("\n=== 3) 重名时必须拒绝，不许默认取第一个 ===")
    x, y, how = _resolve_strong_target("rid=" + str(items[0].get("r")), st)
    ck("★★ 不带序号的 rid= 重名 -> 拒绝", x is None and y is None, how)
    ck("★ 拒绝原因写的是「不唯一」", "不唯一" in how, how)

    print("\n=== 4) 带序号才精确命中 ===")
    base = str(items[0].get("r"))
    x1, y1, h1 = _resolve_strong_target(f"rid={base}#1", st)
    ck("★★ #1 命中第一个格子", x1 is not None, h1)
    ck("★★ #1 的坐标就是第一个格子的中心",
       (x1, y1) == tuple(items[0].get("c") or ()), f"{(x1, y1)} vs {items[0].get('c')}")
    x3, y3, h3 = _resolve_strong_target(f"rid={base}#3", st)
    ck("★ #3 和 #1 不是同一个点", (x3, y3) != (x1, y1), f"{(x3, y3)}")
    ck("★ #3 等于第 3 个格子的中心",
       (x3, y3) == tuple(items[2].get("c") or ()), f"{(x3, y3)}")
    xb, yb, hb = _resolve_strong_target(f"rid={base}#99", st)
    ck("★★ 序号越界 -> 拒绝", xb is None and yb is None, hb)

    print("\n=== 5) 唯一 rid 仍按老规矩走（序号可选）===")
    one = Path(td) / "one.xml"
    one.write_text('<hierarchy><node clickable="true" '
                   'resource-id="com.demo:id/btnOk" text="确定" '
                   'bounds="[0,0][100,100]"/></hierarchy>', encoding="utf-8")
    it2, _ = parse_clickable(one)
    st2 = {"obs_clickable": it2, "obs_xml_path": str(one),
           "width": 3040, "height": 1904, "last_activity": ""}
    xa, ya, ha = _resolve_strong_target("rid=btnOk", st2)
    ck("★ 唯一 rid 不带序号也能命中", xa is not None, ha)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
