#!/usr/bin/env python
"""Provider 健康检查 CLI — issue #76。

部署前/排障时用：区分「配置错误（缺 key）/ 认证失败 / 限流 / 上游不可用」。
不依赖 HTTP 服务，直接用后端 Settings 与 provider 注册表探测。

用法：
    uv run python scripts/check_providers.py            # 检查当前 .env 配置
    uv run python scripts/check_providers.py --timeout 5
退出码：0 = 全部健康；1 = 至少一个 provider 不健康（供 CI/部署门禁使用）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.config import get_settings  # noqa: E402
from backend.core.providers.health import check_all  # noqa: E402

_CODE_HINT = {
    "configuration": "检查 .env 中的 *_api_key / base_url",
    "auth_failed": "API Key 失效或过期，需更换",
    "rate_limited": "上游配额/限流，稍后重试或降并发",
    "timeout": "上游响应超时，检查网络或提高超时设置",
    "unavailable": "上游不可达（网络/服务故障）",
    "invalid_response": "上游返回畸形响应，检查端点兼容性",
}


async def _main(timeout: float | None) -> int:
    settings = get_settings()
    results = await check_all(settings)
    failed = 0
    for h in results:
        mark = "OK  " if h.status == "ok" else "FAIL"
        latency = f"{h.latency_ms}ms" if h.latency_ms is not None else "-"
        print(f"[{mark}] {h.kind:<9} {h.provider:<14} model={h.model} "
              f"latency={latency}")
        if h.status != "ok":
            failed += 1
            print(f"       code={h.code} detail={h.detail}")
            hint = _CODE_HINT.get(h.code or "")
            if hint:
                print(f"       -> {hint}")
        caps = h.capabilities.as_dict() if h.capabilities else {}
        if caps:
            print(f"       capabilities: structured_output="
                  f"{caps.get('structured_output')} "
                  f"native_json_schema={caps.get('native_json_schema')} "
                  f"max_batch={caps.get('max_batch')}")
    print(f"\n{len(results) - failed}/{len(results)} healthy")
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Provider health check")
    parser.add_argument("--timeout", type=float, default=None,
                        help="单次探测超时（秒），默认取配置")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.timeout)))
