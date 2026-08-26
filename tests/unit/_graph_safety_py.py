"""Python 镜像 frontend/graph_safety.ts —— 供 CT-03 单测验证过滤语义。

两份实现必须保持同一规则集：
  1. 节点必须有字符串 id；
  2. 边两端必须指向已知节点，否则丢弃并告警；
  3. 自环边丢弃并告警。
"""
from __future__ import annotations

import logging

log = logging.getLogger("frontend.graph_safety")


def build_flow_safe(subgraph: dict) -> dict:
    nodes = [n for n in subgraph.get("nodes") or []
             if isinstance(n, dict) and isinstance(n.get("id"), str)]
    ids = {n["id"] for n in nodes}
    edges = []
    for e in subgraph.get("edges") or []:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            continue
        if e.get("source") not in ids or e.get("target") not in ids:
            log.warning("invalid edge reference dropped %s", {"edge_id": e["id"]})
            continue
        if e["source"] == e["target"]:
            log.warning("self-loop edge dropped %s", {"edge_id": e["id"]})
            continue
        edges.append(e)
    return {"rfNodes": nodes, "rfEdges": edges}
