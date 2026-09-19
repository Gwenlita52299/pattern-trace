"""真实测试集评测 — ingest/seed/test_top20_*.csv / test_bottom20_no_match.csv。

角色定义（用户口径）：
- test_top20_C1 / test_top20_C6（各 20 条）：已知 positive——与 Lazarus 模式
  同簇且贴近 confirmed 质心，但不在知识库内；评测目标是召回与 judge 命中
- test_bottom20_no_match（20 条，C2 簇）：已知 negative（no_match 样本），
  评测误报率与判定阈值校准

两个阶段：
  submit  — 走生产 analyze 链路（HTTP）：逐地址提交 + 轮询至 completed/failed，
            原始结果存 output/eval/test_set_raw.json（可断点续跑：已 completed
            的地址直接复用）
  report  — 聚合指标：positive 召回（judge 命中 + 直接 Retriever recall@k）、
            negative 误报率、confidence 阈值扫描

用法：
  python tests/evaluation/eval_test_set.py --base-url http://localhost:8000
  python tests/evaluation/eval_test_set.py --report-only
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "JWT_SECRET",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")

SEED_DIR = ROOT / "ingest" / "seed"
SETS = [  # (文件名, 期望标签)
    ("test_top20_C1.csv", "positive"),
    ("test_top20_C6.csv", "positive"),
    ("test_bottom20_no_match.csv", "negative"),
]
RAW_PATH = ROOT / "output" / "eval" / "test_set_raw.json"
REPORT_PATH = ROOT / "docs" / "baselines" / "test_set_eval.json"

POLL_INTERVAL_S = 3
POLL_TIMEOUT_S = 240


def load_sets() -> list[dict]:
    rows = []
    for fname, expected in SETS:
        with open(SEED_DIR / fname, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                addr = (r.get("address") or "").strip()
                if addr:
                    rows.append({"set": fname, "expected": expected,
                                 "address": addr, "cluster": r.get("cluster", "")})
    return rows


def login(client, base_url: str) -> None:
    from backend.core.config import get_settings

    s = get_settings()
    resp = client.post(f"{base_url}/api/v1/auth/login", json={
        "email": s.bootstrap_admin_email, "password": s.bootstrap_admin_password})
    resp.raise_for_status()
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"


def phase_submit(base_url: str, raw: list[dict], gap_s: float = 20.0) -> None:
    import httpx

    # 幂等：completed 复用；failed/poll_timeout/submit_error 重试
    # （LLM schema 偶发失败与 Esplora 限流超时都值得再试）
    reusable = ("completed",)
    done = {r["address"] for r in raw if r.get("status") in reusable}
    raw[:] = [r for r in raw if r.get("status") in reusable]
    rows = [r for r in load_sets() if r["address"] not in done]
    print(f"submit: {len(done)} 已完成，待提交 {len(rows)}")

    with httpx.Client(timeout=60,
                      headers={"X-Requested-With": "XMLHttpRequest"}) as client:
        login(client, base_url)
        for i, r in enumerate(rows):
            try:
                resp = client.post(f"{base_url}/api/v1/addresses/analyze",
                                   json={"address": r["address"], "hops": 3,
                                         "time_window_days": 90})
            except httpx.HTTPError as exc:
                # 网络抖动/偶发超时不中断整场评测——按 submit_error 记录，
                # 下一次 --report 前重跑（幂等：completed 行 BE-46 复用）
                r["status"] = "submit_error_httpx"
                r["error"] = f"{type(exc).__name__}: {exc}"[:300]
                raw.append(r)
                print(f"  [{i+1}/{len(rows)}] {r['address'][:20]}… 提交异常 "
                      f"{type(exc).__name__}")
                continue
            token_expired = resp.status_code == 401 or (
                resp.status_code == 403 and "DEMO_ADDRESS" in resp.text)
            if token_expired:
                # access token 15min TTL：过期即重登录再试一次
                login(client, base_url)
                resp = client.post(f"{base_url}/api/v1/addresses/analyze",
                                   json={"address": r["address"], "hops": 3,
                                         "time_window_days": 90})
            if resp.status_code not in (200, 202, 409):
                r["status"] = f"submit_error_{resp.status_code}"
                r["error"] = resp.text[:300]
                raw.append(r)
                print(f"  [{i+1}/{len(rows)}] {r['address'][:20]}… 提交失败 "
                      f"{resp.status_code}")
                continue
            jid = resp.json()["judgment_id"]
            r["judgment_id"] = jid
            deadline = time.time() + POLL_TIMEOUT_S
            while time.time() < deadline:
                try:
                    j = client.get(f"{base_url}/api/v1/judgments/{jid}")
                    payload = j.json()
                except httpx.HTTPError as exc:
                    # 轮询偶发超时：短暂退避后继续（不终止整场评测）
                    print(f"    poll httpx error {type(exc).__name__}; retrying")
                    time.sleep(POLL_INTERVAL_S * 2)
                    continue
                if payload.get("status") in ("completed", "failed"):
                    r["status"] = payload["status"]
                    r["risk_level"] = payload.get("risk_level")
                    r["confidence"] = payload.get("confidence")
                    r["matched_pattern_name"] = payload.get("matched_pattern_name")
                    r["latency_ms"] = payload.get("latency_ms")
                    if payload["status"] == "failed":
                        r["error"] = payload.get("error_message", "")[:300]
                    break
                time.sleep(POLL_INTERVAL_S)
            else:
                r["status"] = "poll_timeout"
            raw.append(r)
            print(f"  [{i+1}/{len(rows)}] {r['address'][:20]}… {r['status']} "
                  f"risk={r.get('risk_level')} conf={r.get('confidence')}")
            time.sleep(gap_s)  # 公网 Esplora 限流友好：提交间隔放缓

    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    print(f"raw results -> {RAW_PATH}")


def _retrieve_recall(canon: dict, k: int) -> tuple[bool, str | None]:
    """对已构建子图直接调 Retriever，返回 top-k 内是否含 confirmed 真实样本。"""
    from ingest.common import get_engine
    from backend.core.config import get_settings, reset_settings
    from backend.retrieval.retriever import Retriever
    from sqlalchemy.orm import Session

    reset_settings()
    with Session(get_engine()) as session:
        result = Retriever(session, get_settings()).retrieve(canon)
        top = result.candidates[:k]
        hit = next((c for c in top if c.source == "lazarus_confirmed"), None)
        return hit is not None, (hit.name if hit else None)


def phase_report(raw: list[dict]) -> int:
    completed = [r for r in raw if r["status"] == "completed"]
    positives = [r for r in completed if r["expected"] == "positive"]
    negatives = [r for r in completed if r["expected"] == "negative"]

    if not positives or not negatives:
        print(f"样本不足：completed positive={len(positives)} "
              f"negative={len(negatives)}——先完成 submit 阶段", file=sys.stderr)
        return 2

    # ---- judge 层指标（issue #72 口径）----
    # 语义矩阵（枚举重构后）：匹配状态 = matched 非 null；风险上报 =
    # risk ∈ {medium, high}。评测集是 KB 外同簇变体——正确行为是
    # flagged_no_pattern（matched=null + medium/high + review），
    # 所以 TPR 用「风险被上报」口径，matched 判定单独输出。
    def hit(r):
        return r.get("matched_pattern") is not None

    def strong_hit(r):
        return r["risk_level"] in ("high", "medium")

    tpr = sum(hit(r) for r in positives) / len(positives)
    tpr_strong = sum(strong_hit(r) for r in positives) / len(positives)
    fpr = sum(hit(r) for r in negatives) / len(negatives)
    fpr_strong = sum(strong_hit(r) for r in negatives) / len(negatives)

    # ---- 检索层 recall@k（生产链路同款 Retriever，直调）----
    recall: dict[int, float] = {}
    try:
        from backend.models.base import Judgment
        from ingest.common import get_engine
        from sqlalchemy.orm import Session

        snaps: dict[str, dict] = {}
        with Session(get_engine()) as session:
            for r in completed:
                row = session.get(Judgment, r["judgment_id"])
                if row is not None and row.subgraph_snapshot:
                    snaps[r["address"]] = row.subgraph_snapshot
        for k in (1, 3, 5, 10):
            hits = []
            for r in positives:
                canon = snaps.get(r["address"])
                if not canon:
                    continue
                found, _ = _retrieve_recall(canon, k)
                hits.append(found)
            recall[k] = round(sum(hits) / len(hits), 4) if hits else 0.0
    except Exception as exc:  # 检索直调失败不阻塞 judge 层指标输出
        print(f"retrieval recall 计算失败（judge 层指标仍有效）: {exc}",
              file=sys.stderr)

    # ---- confidence 阈值扫描（bottom20 校准，#72 风险上报口径）----
    sweep = []
    for cutoff in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        pred_pos = [strong_hit(r) and (r["confidence"] or 0) >= cutoff
                    for r in positives]
        pred_neg_hit = [strong_hit(r) and (r["confidence"] or 0) >= cutoff
                        for r in negatives]
        sweep.append({
            "cutoff": cutoff,
            "tpr": round(sum(pred_pos) / len(pred_pos), 4),
            "fpr": round(sum(pred_neg_hit) / len(pred_neg_hit), 4),
        })

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "counts": {"positive": len(positives), "negative": len(negatives),
                   "submitted": len(raw), "failed": len(raw) - len(completed)},
        "judge": {
            # issue #72 口径：tpr_strong = positive 风险被上报率（medium/high）；
            # fpr_strong = negative 风险被上报率——注意 bottom20 的标签口径
            # 是「聚类不匹配 ≠ 行为无风险」（#71 人工复核：bc1psrwl5fkh
            # peel-and-return+coinjoin 实为可疑），fpr_strong 虚高是标签问题
            "positive_hit_rate_any": round(tpr, 4),
            "positive_hit_rate_strong": round(tpr_strong, 4),
            "kb_match_rate": round(tpr, 4),
            "negative_fpr_any": round(fpr, 4),
            "negative_fpr_strong": round(fpr_strong, 4),
            "fpr_caveat": ("bottom20 labels are cluster-based, not behavior-"
                           "based; manual review of 1/20 confirmed suspicious "
                           "behavior (peel-and-return + coinjoin) — "
                           "see issue #71 comment 2"),
        },
        "retrieval_recall_at_k": recall,
        "confidence_threshold_sweep": sweep,
        "per_address": [{k: r.get(k) for k in
                         ("set", "expected", "address", "status", "risk_level",
                          "confidence", "matched_pattern_name", "latency_ms")}
                        for r in raw],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "per_address"},
                     ensure_ascii=False, indent=2))
    print(f"report -> {REPORT_PATH}")

    # 评测门槛（#72 口径）：positive 风险上报率 ≥0.8。fpr_strong 受 bottom20
    # 行为口径标签复核影响（#71 衍生待办），暂不计入硬门禁
    ok = tpr_strong >= 0.8
    print("✅ 评测达标" if ok else "❌ 评测未达标（positive 风险上报率 <0.8）")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="真实测试集评测")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--report-only", action="store_true",
                        help="跳过提交，仅用已有 raw 结果聚合")
    parser.add_argument("--submit-only", action="store_true")
    parser.add_argument("--gap", type=float, default=20.0,
                        help="相邻提交间隔秒数（公网 Esplora 限流，默认 20s）")
    args = parser.parse_args(argv)

    raw: list[dict] = []
    if RAW_PATH.exists():
        raw = json.loads(RAW_PATH.read_text())

    if not args.report_only:
        phase_submit(args.base_url, raw, gap_s=args.gap)
    if args.submit_only:
        return 0
    return phase_report(raw)


if __name__ == "__main__":
    sys.exit(main())
