"""阶段6 契约收口 — CT-01 / CT-02 / CT-03。

CT-01：OpenAPI schema 快照守护（与前端 types/api.d.ts 同步生成）；
CT-02：不存在 judgment 稳定 404 + poll_url 相对路径契约；
CT-03：畸形 subgraph（edge 指向缺失节点）防御——builder 校验层 + 渲染层过滤。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "frontend" / "types" / "api.d.ts"
SNAPSHOT_PATH = Path(__file__).parent / "__snapshots__" / "openapi.json"


# ---- CT-01 -----------------------------------------------------------
def _canonical_openapi() -> dict:
    from backend.api.app import create_app

    spec = create_app().openapi()
    # 路径顺序稳定化，避免 dict 序影响快照 diff
    return {"paths": {k: spec["paths"][k] for k in sorted(spec["paths"])},
            "components": spec.get("components", {})}


def test_ct01_openapi_snapshot_guard():
    """schema 变更必须显式更新快照并同步 codegen —— 双向漂移检测。"""
    current = json.dumps(_canonical_openapi(), sort_keys=True,
                         separators=(",", ":"))
    assert SNAPSHOT_PATH.exists(), (
        "缺少 OpenAPI 快照；运行 `python -m scripts.gen_api_types` 初始化")
    stored = SNAPSHOT_PATH.read_text()
    assert current == stored, (
        "OpenAPI schema drift detected (CT-01). "
        "Run: python -m scripts.gen_api_types && "
        "(cd frontend && npm run type-check)")


def test_ct01_codegen_output_in_sync():
    if not SPEC_PATH.exists():
        pytest.skip("frontend/types/api.d.ts not generated yet")
    from backend.api.app import create_app

    expected = create_app().openapi()
    text = SPEC_PATH.read_text()
    for path in expected["paths"]:
        assert f"'{path}'" in text or f'"{path}"' in text, \
            f"codegen missing path {path} (CT-01 reverse direction)"


# ---- CT-02 -----------------------------------------------------------
def _db_available() -> bool:
    try:
        from sqlalchemy import text as _t

        from backend.api.app import get_db_engine

        with get_db_engine().connect() as conn:
            conn.execute(_t("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not _db_available(), reason="PostgreSQL 未运行（verify_phase6.sh 中覆盖）")


@requires_db
def test_ct02_nonexistent_judgment_stable_404(client):
    import uuid

    missing = str(uuid.uuid4())
    for i in range(10):
        r = client.get(f"/api/v1/judgments/{missing}")
        assert r.status_code == 404, f"poll #{i + 1}: {r.status_code}"
        body = r.json()
        assert body.get("error_code") == "NOT_FOUND"


@requires_db
def test_ct02_poll_url_is_relative_path(client):
    """analyze 响应的 poll_url 必须是相对路径，由前端拼 API_BASE。"""
    seeds = client.get("/api/v1/demo/addresses").json()["addresses"]
    r = client.post("/api/v1/addresses/analyze",
                    json={"address": seeds[0]})
    assert r.status_code == 202
    poll = r.json()["poll_url"]
    assert poll.startswith("/api/v1/judgments/"), poll


# ---- CT-03 -----------------------------------------------------------
class _N:
    def __init__(self, nid, kind="address", label="x"):
        self.id = nid
        self.kind = kind
        self.label = label


class _E:
    def __init__(self, eid, src, dst):
        self.id = eid
        self.source = src
        self.target = dst


def test_ct03_builder_layer_rejects_dangling_edges():
    """builder 输出校验层：edge 引用不存在的 node → ValidationError。"""
    from pydantic import ValidationError

    from backend.retrieval.retriever import validate_canonical_subgraph

    bad = {
        "seed_address": "seed",
        "nodes": [{"id": "addr:a", "kind": "address"}],
        "edges": [{"id": "edge:x", "source": "addr:a",
                   "target": "addr:ghost"}],
        "stats": {"node_count": 1, "edge_count": 1},
    }
    with pytest.raises(ValidationError):
        validate_canonical_subgraph(bad)


def test_ct03_render_layer_filters_dangling_edges():
    """渲染防线：GraphCanvas 构图函数静默过滤无效边并留日志。

    前端实现是 TS（frontend/graph_safety.ts），此处以等价 Python 镜像验证
    过滤语义；真实浏览器行为由 Playwright mock 用例覆盖（CT-03 步骤2）。
    """
    import logging

    from tests.unit._graph_safety_py import build_flow_safe

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("frontend.graph_safety")
    logger.addHandler(handler)
    try:
        subgraph = {
            "nodes": [{"id": "addr:a", "kind": "address", "first_layer": 0}],
            "edges": [{"id": "e1", "source": "addr:a", "target": "addr:ghost"},
                      {"id": "e2", "source": "addr:a", "target": "addr:b"}],
        }
        result = build_flow_safe(subgraph)
    finally:
        logger.removeHandler(handler)
    assert len(result["rfEdges"]) == 0 and len(result["rfNodes"]) == 1
    assert any("invalid edge reference" in r.message for r in records)
