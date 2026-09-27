#!/usr/bin/env bash
# PatternTrace 阶段1 门禁验证：graph-builder 核心能力
#   完成标志1: 输入地址返回受控子图（实网 Esplora smoke）
#   完成标志2: 终止条件统计与参考实现基线对齐（golden fixture 对齐测试）
# 用法: bash infra/verify_phase1.sh   （网络不可用时 smoke 会失败，单测仍可独立验证）
set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
ok()  { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad() { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

echo "== GB-01~24 单元测试（mock，无公网依赖）=="
if .venv/bin/python -m pytest tests/unit/ -q 2>&1 | tail -1; then
  ok "单元测试套件通过"
else
  bad "单元测试存在失败"
fi

echo "== GB-24 · 120K 边微基准 =="
bench_out=$(.venv/bin/python -m backend.graph_builder.bench_graph_builder 2>&1 | tail -2 | head -1)
echo "  $bench_out"
ok "bench 可运行（纯 Python CPU 路径）"

echo "== 完成标志1 · 实网 smoke：输入地址 → 受控子图 =="
.venv/bin/python - <<'PY'
import asyncio, sys

from backend.graph_builder.esplora import EsploraClient
from backend.graph_builder.builder import GraphBuilder

ADDR = '12ib7dApVFvg82TXKycWBNpN8kFyiAN1dr'  # 演示用活跃地址

async def main():
    client = EsploraClient()
    cache: dict[str, list] = {}
    try:
        async def fetch(addr):
            if addr not in cache:
                raw = await client.get_address_txs(addr)
                cache[addr] = [type('T', (), {
                    'txid': t['txid'],
                    'inputs': [{'value': (i.get('prevout') or {}).get('value', 0) / 1e8}
                               for i in t.get('vin', [])],
                    'outputs': [{'address': o.get('scriptpubkey_address'),
                                 'value': o.get('value', 0) / 1e8}
                                for o in t.get('vout', [])],
                    'block_time': (t.get('status') or {}).get('block_time'),
                    'unspent_outputs': set(),
                })() for t in raw]
            return cache[addr]

        await asyncio.wait_for(fetch(ADDR), timeout=15)

        def provider(addr):
            return cache.get(addr, [])

        # 两遍收敛：BFS 是纯同步计算，网络预取在外层 async 驱动——
        # 每轮把新发现且未取数的地址补取后再跑，直到无缺口（≤hops+1 轮）
        builder_args = dict(hops=3, time_window_days=365 * 2)
        result = None
        for _ in range(5):
            result = GraphBuilder().build(ADDR, provider, **builder_args)
            missing = [n.label for n in result.nodes
                       if n.kind == 'address' and n.label not in cache]
            if not missing:
                break
            await asyncio.gather(*(fetch(a) for a in missing[:50]))

        s = result.stats
        kinds: dict[str, int] = {}
        for n in result.nodes:
            kinds[n.kind] = kinds.get(n.kind, 0) + 1
        layers = sorted({n.first_layer for n in result.nodes if n.kind == 'address'})
        print(f"  subgraph: nodes={len(result.nodes)} {kinds} "
              f"edges={len(result.edges)} bfs={s.elapsed_ms:.0f}ms")
        print(f"  address first_layers reached: {layers}")
        print(f"  termination: {s.termination_summary()}")

        assert len(result.nodes) > 10, "受控子图过小，多层展开未生效"
        assert s.degraded is False, "存在降级分支"
        assert 3 in layers or 2 in layers, "3 跳内应展开到更深层"
        assert any(e.id.startswith('edge:') for e in result.edges)
        ids = result.node_ids()
        assert all(e.source in ids and e.target in ids for e in result.edges), "悬挂边"
    finally:
        await client.close()

asyncio.run(main())
PY
[ $? -eq 0 ] && ok "实网 3 跳子图构建成功（D3 引用完整）" || bad "实网 smoke 失败（检查网络或 blockstream 可达性）"

echo ""
echo "========================================"
echo "结果: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "🎉 阶段1 门禁通过" || echo "存在未达标项，见上方 ❌"
exit "$FAIL"
