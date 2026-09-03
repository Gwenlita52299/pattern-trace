"""Python 镜像 frontend/src/components/GraphCanvas.tsx —— 折叠/展开的下游可达隐藏逻辑。

两份实现必须保持同一规则集（与 GraphCanvas.tsx::computeDescendants /
::hiddenSet 一致）：

  1. `compute_descendants` 返回从某节点沿有向边可达的**严格下游**节点集合
     （不含起始节点本身）。地址流虽近似有向无环，但自转账/回流地址可能形成
     自环或环路（A→A / A→B→A）：把起始节点预先标记为已访问，避免环路或
     自环回到起点时把起始节点计入结果，同时避免死循环。
  2. `hidden_set` 返回所有「已折叠」节点下游节点的并集（这些节点应隐藏）。
     折叠节点自身必须保持可见（以便用户再次展开并恢复下游），即使它们互相
     位于对方的下游中，因此从并集中剔除所有已折叠节点。
"""
from __future__ import annotations


def compute_descendants(flow: dict, node_id: str) -> set[str]:
    """从 node_id 出发沿有向边可达的严格下游节点集合（不含 node_id 本身）。"""
    children: dict[str, list[str]] = {}
    for e in flow.get("edges", []):
        children.setdefault(e["source"], []).append(e["target"])

    out: set[str] = set()
    visited = {node_id}  # 起始节点视为已访问：自环/环路不会重新计入结果
    stack = [node_id]
    while stack:
        cur = stack.pop()
        for c in children.get(cur, []):
            if c not in visited:
                visited.add(c)
                out.add(c)
                stack.append(c)
    return out


def hidden_set(flow: dict, collapsed: set[str]) -> set[str]:
    """所有已折叠节点下游节点集合的并集——这些节点当前应隐藏。

    折叠节点自身从隐藏集合中剔除（保持可见，便于再次展开）。
    """
    hidden: set[str] = set()
    for node_id in collapsed:
        hidden |= compute_descendants(flow, node_id)
    hidden -= collapsed
    return hidden
