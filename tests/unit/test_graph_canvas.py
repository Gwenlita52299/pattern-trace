"""折叠/展开下游可达隐藏逻辑（issue #6）—— 镜像 GraphCanvas.tsx::computeDescendants / ::hiddenSet。

验证核心语义：
  1. compute_descendants 返回**严格下游**，绝不包含起始节点自身；
  2. 自环（A→A）与环路（A→B→A）不会死循环，也不会把起始节点计入结果；
  3. hidden_set 取所有折叠节点下游的并集，但把折叠节点自身从隐藏集合中剔除
     （折叠节点保持可见，便于再次展开并恢复下游）；
  4. 多个节点分别折叠时，隐藏集合按并集计算，不误隐藏折叠节点自身。

与 GraphCanvas.tsx 保持同一规则集。若 TS 实现变动，此镜像需同步更新。
"""
from __future__ import annotations

from tests.unit._graph_canvas_py import compute_descendants, hidden_set


def _flow(edges):
    """构造只有边（source/target 为地址节点 id）的流式图，供可达/隐藏计算。"""
    return {"nodes": [], "edges": edges}


def test_self_loop_excludes_start_node():
    # 自转账：A → A。折叠 A 后 A 必须仍然可见（A 不在自己的下游里）。
    flow = _flow([{"id": "e1", "source": "addr:A", "target": "addr:A"}])
    assert compute_descendants(flow, "addr:A") == set()
    assert hidden_set(flow, {"addr:A"}) == set()


def test_cycle_excludes_start_node_and_no_infinite_loop():
    # A → B → A 环路。折叠 A 只隐藏严格下游 B，A 不被隐藏。
    flow = _flow([
        {"id": "e1", "source": "addr:A", "target": "addr:B"},
        {"id": "e2", "source": "addr:B", "target": "addr:A"},
    ])
    assert compute_descendants(flow, "addr:A") == {"addr:B"}
    assert compute_descendants(flow, "addr:B") == {"addr:A"}
    assert hidden_set(flow, {"addr:A"}) == {"addr:B"}


def test_cycle_collapse_both_nodes_keeps_anchor_visible():
    # 环路 A→B→A 中同时折叠 A 与 B：两者互为对方下游，但折叠节点自身均须可见。
    flow = _flow([
        {"id": "e1", "source": "addr:A", "target": "addr:B"},
        {"id": "e2", "source": "addr:B", "target": "addr:A"},
    ])
    assert hidden_set(flow, {"addr:A", "addr:B"}) == set()


def test_chain_strict_descendants():
    # 链式 A→B→C→D：折叠 A 隐藏 B/C/D，A 自身保持可见。
    flow = _flow([
        {"id": "e1", "source": "addr:A", "target": "addr:B"},
        {"id": "e2", "source": "addr:B", "target": "addr:C"},
        {"id": "e3", "source": "addr:C", "target": "addr:D"},
    ])
    assert compute_descendants(flow, "addr:A") == {"addr:B", "addr:C", "addr:D"}
    assert hidden_set(flow, {"addr:A"}) == {"addr:B", "addr:C", "addr:D"}


def test_collapse_leaf_node_hides_nothing_of_itself():
    # 折叠最尾的 D：无下游，D 不受影响（D 保持可见）。
    flow = _flow([
        {"id": "e1", "source": "addr:A", "target": "addr:B"},
        {"id": "e2", "source": "addr:B", "target": "addr:C"},
        {"id": "e3", "source": "addr:C", "target": "addr:D"},
    ])
    assert compute_descendants(flow, "addr:D") == set()
    assert hidden_set(flow, {"addr:D"}) == set()


def test_multiple_collapse_union_excludes_anchors():
    # 折叠 A 与 C（A→B→C→D）：隐藏集合 = (A 下游) ∪ (C 下游)，且剔除折叠节点自身。
    flow = _flow([
        {"id": "e1", "source": "addr:A", "target": "addr:B"},
        {"id": "e2", "source": "addr:B", "target": "addr:C"},
        {"id": "e3", "source": "addr:C", "target": "addr:D"},
    ])
    # A 下游 {B,C,D}，C 下游 {D}；并集 {B,C,D}，剔除折叠集 {A,C} → {B,D}
    assert hidden_set(flow, {"addr:A", "addr:C"}) == {"addr:B", "addr:D"}


def test_cycle_with_branch_and_leaf():
    # A→B→A 环，同时有 A→C→D 支路：折叠 A 隐藏 B、C、D（A 自身除外）。
    flow = _flow([
        {"id": "e1", "source": "addr:A", "target": "addr:B"},
        {"id": "e2", "source": "addr:B", "target": "addr:A"},
        {"id": "e3", "source": "addr:A", "target": "addr:C"},
        {"id": "e4", "source": "addr:C", "target": "addr:D"},
    ])
    assert compute_descendants(flow, "addr:A") == {"addr:B", "addr:C", "addr:D"}
    assert hidden_set(flow, {"addr:A"}) == {"addr:B", "addr:C", "addr:D"}
