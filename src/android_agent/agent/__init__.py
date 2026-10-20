"""Agent 层：LangGraph 状态机与决策通道。

模块职责：

    nodes.py        子 Agent 的节点定义（输入 ChildState，输出要更新的字段）
    graph.py        用例级图（act/judge/expect/verdict/gate/finish）
    runner.py       跑 interrupt 循环的驱动，负责跟模型来回
    decide.py       决策通道：把 payload 渲染成 prompt、校验模型回复
    vision.py       看图通道（不依赖主模型的能力）
    child_runner.py 断点续跑

⚠️ `agent/__init__.py` **故意不 import 任何子模块**。
   早先在这里写了 `from .graph import build_graph`，结果是：
   任何 `import android_agent.agent.nodes` 都会先把 graph.py 执行一遍，
   而 graph.py 又要 import device.bridge —— 于是把整条依赖链拽进来，
   连只想要数据类（StepSpec 之类）的场景也被迫加载 LangGraph。
   而且一旦某个子模块有循环依赖，**整包都 import 失败**，报错位置
   还指向 __init__，根本看不出真正的问题在哪。

   所以这里只放说明。要用什么，显式地从对应子模块 import：
       from android_agent.agent.nodes import StepSpec
"""

from __future__ import annotations

__all__: list[str] = []
