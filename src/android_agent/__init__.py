"""Android GUI 自动化测试执行器。

这个包把「在真机上按用例操作 App」这件事拆成四层，每层只关心自己的事：

    cases/    用例是什么 —— 读 YAML、解析成结构化 spec、算路径
    device/   怎么碰设备 —— 工具层桥接、会话锁、调试探针
    agent/    怎么跑 —— LangGraph 状态机、节点、决策通道
    judge/    怎么判 —— 落点判定、断言求值
    report/   怎么记 —— YAML 回写

分层的意义不只是好看：**判定逻辑（judge）必须能脱离设备单独测试**。
它是"哪些能由代码确定、哪些必须交给 AI"这条分工的落点，
所以它不能 import device，只能吃纯数据。
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
