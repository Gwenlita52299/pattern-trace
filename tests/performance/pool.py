"""压测轮询 id 池 — 读写与抽样的纯函数（stress-test-spec §2.3）。

为什么单独成模块：池文件的格式与抽样规则要被三处共用——`run_stress.py`
（生成）、`locustfile.py`（消费）、单元测试（校验合法性）。放在这里不依赖
locust/SQLAlchemy，因此能进常规单测（CI 的 unit job 也装了 dev extras，但
"能不依赖就不依赖"更稳）。

池的两档口径（spec §2.3，不可合并统计）：
    hot   最近 1 小时内完成的判定（模拟刚分析完的轮询）
    cold  全表随机（模拟历史案件回看）

没有池文件时 locust 侧退化为 API 派生池（近似热集），该退化只允许冒烟，
不得作为基线——`derived` 字段就是给归档记录这一事实用的。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

HOT = "hot"
COLD = "cold"
TIERS = (HOT, COLD)
ADDRESSES = "addresses"


def write_pool(path: str | Path, *, hot: list[str], cold: list[str],
               addresses: list[str] | None = None,
               meta: dict | None = None) -> Path:
    """写池文件。meta 记录数据规模/窗口等自描述信息（归档需要）。

    addresses 供 subgraph 任务采样（judgment id 无法直接推导地址，见 §2.3）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hot": list(hot),
        "cold": list(cold),
        ADDRESSES: list(addresses or []),
        "meta": dict(meta or {}),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return p


def load_pool(path: str | Path) -> dict:
    """读池文件并校验结构；不合法直接抛错（宁可拒绝运行，不要静默跑出假数据）。"""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"pool file must be a JSON object: {path}")
    for tier in TIERS:
        values = data.get(tier, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError(f"pool tier {tier!r} must be a list[str]: {path}")
    data.setdefault(ADDRESSES, [])
    data.setdefault("meta", {})
    return data


def pool_usable_as_baseline(pool: dict) -> bool:
    """基线口径要求热/冷两档都非空（spec §2.3）。"""
    return bool(pool.get(HOT)) and bool(pool.get(COLD))


def sample(pool: dict, tier: str, rng: random.Random | None = None) -> str | None:
    """从指定档位随机取一个 id；档位为空返回 None（调用方跳过该次请求）。"""
    values = pool.get(tier) or []
    if not values:
        return None
    return (rng or random).choice(values)


def sample_address(pool: dict, rng: random.Random | None = None) -> str | None:
    """从池内地址子集取一个地址（subgraph 任务用）；无地址时返回 None。"""
    values = pool.get(ADDRESSES) or []
    if not values:
        return None
    return (rng or random).choice(values)
