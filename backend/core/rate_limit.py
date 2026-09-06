"""多实例共享的原子限流器（backend-api-spec §5 · core/rate_limit.py）。

固定窗口计数（INCR + EXPIRE）：窗口起点为首次命中，到期自动清零。相比真
令牌桶牺牲突发平滑，换取单命令原子性与 O(1) 内存——满足 spec §5「Redis
令牌桶 + 匿名 IP / 登录账号双轨限流」的语义。

Fail 策略（issue #23 验收项）：Redis 不可达时 fail-open 降级进程内计数——
可用性优先，单实例限流仍然生效，仅多实例共享失效；降级事件打印留痕
（每进程只记一次，避免日志风暴）。登录爆破防护同策略。
"""
from __future__ import annotations

import time

_force_memory = False   # 测试隔离：conftest 置 True 后跳过 Redis 建连
_limiter: "RedisRateLimiter | None" = None

# 运行中 Redis 掉线的重试冷却：期间直接走 memory，避免每请求阻塞在超时上
# （对齐 task_queue 的 _fail_until 模式；构造期降级只覆盖「启动时就不在」形态）
_REDIS_COOLDOWN = 5.0


class RedisRateLimiter:
    """按 (bucket, identity) 维度的固定窗口计数限流。

    bucket 是用途命名空间（如 anon_rate / login_ip），identity 是限流主体
    （IP / email）。计数 key 形如 rl:{bucket}:{identity}，TTL 即窗口。
    """

    def __init__(self, redis_url: str = "", forced_memory: bool = False):
        self._memory: dict[str, tuple[int, float]] = {}  # key -> (count, expires_at)
        self._redis = None
        self._down_until = 0.0  # monotonic 时间戳：冷却期内跳过 Redis 直接降级
        if redis_url and not forced_memory:
            try:
                import redis

                client = redis.Redis.from_url(redis_url,
                                              socket_connect_timeout=1)
                client.ping()
                self._redis = client
            except Exception as exc:  # noqa: BLE001 — 降级是合法运行态
                self._redis = None
                print(f"[rate_limit] redis unavailable "
                      f"({exc.__class__.__name__}); fail-open to "
                      "in-process counting (multi-instance sharing disabled)")

    @staticmethod
    def _key(bucket: str, identity: str) -> str:
        return f"rl:{bucket}:{identity}"

    def _redis_ok(self) -> bool:
        return self._redis is not None and time.monotonic() >= self._down_until

    def _mark_redis_down(self, exc: Exception) -> None:
        self._down_until = time.monotonic() + _REDIS_COOLDOWN
        print(f"[rate_limit] redis op failed ({exc.__class__.__name__}); "
              "fail-open to in-process counting (multi-instance "
              "sharing disabled)")

    def check(self, bucket: str, identity: str,
              limit: int) -> tuple[bool, int]:
        """只读检查当前计数是否已达上限（不递增）。返回 (allowed, retry_after)。"""
        if self._redis_ok():
            k = self._key(bucket, identity)
            try:
                count = self._redis.get(k)
                if count is None:
                    return True, 0
                count = int(count)
                if count < limit:
                    return True, 0
                ttl = self._redis.ttl(k)
                return False, max(1, ttl if ttl > 0 else 60)
            except Exception as exc:  # noqa: BLE001 — 运行中掉线即降级
                self._mark_redis_down(exc)
        count, expires_at = self._memory.get(
            self._key(bucket, identity), (0, 0.0))
        if time.time() >= expires_at:
            return True, 0
        if count < limit:
            return True, 0
        return False, max(1, int(expires_at - time.time()))

    def hit(self, bucket: str, identity: str, *, limit: int,
            window: int) -> tuple[bool, int]:
        """计数 +1 并判断是否超限。返回 (allowed, retry_after)。"""
        if self._redis_ok():
            k = self._key(bucket, identity)
            try:
                count = self._redis.incr(k)
                if count == 1:
                    self._redis.expire(k, window)
                if count <= limit:
                    return True, 0
                ttl = self._redis.ttl(k)
                return False, max(1, ttl if ttl > 0 else window)
            except Exception as exc:  # noqa: BLE001 — 运行中掉线即降级
                self._mark_redis_down(exc)
        k = self._key(bucket, identity)
        now = time.time()
        count, expires_at = self._memory.get(k, (0, 0.0))
        if now >= expires_at:  # 窗口过期：从 1 重新起算
            count, expires_at = 0, now + window
        count += 1
        self._memory[k] = (count, expires_at)
        if count <= limit:
            return True, 0
        return False, max(1, int(expires_at - now))

    def reset(self, bucket: str, identity: str) -> None:
        """成功认证后清零计数（避免正常用户被历史失败拖累）。"""
        if self._redis_ok():
            try:
                self._redis.delete(self._key(bucket, identity))
                return
            except Exception as exc:  # noqa: BLE001 — 运行中掉线即降级
                self._mark_redis_down(exc)
        self._memory.pop(self._key(bucket, identity), None)


def get_rate_limiter() -> RedisRateLimiter:
    """进程级单例；Redis 建连失败时实例内部已降级，调用方无感知。"""
    global _limiter
    if _limiter is None:
        from .config import get_settings

        _limiter = RedisRateLimiter(
            get_settings().redis_url, forced_memory=_force_memory)
    return _limiter


def reset_rate_limiter() -> None:
    """重建单例（测试隔离 / 配置热更后重读 redis_url）。"""
    global _limiter
    from .config import get_settings

    _limiter = RedisRateLimiter(
        "" if _force_memory else get_settings().redis_url,
        forced_memory=_force_memory)
