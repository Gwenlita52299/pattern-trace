"""JWT + 密码哈希 — backend-api-spec §5 安全设计.

密码哈希用真 bcrypt（cost 12）；JWT 用 HMAC-SHA256 手写（格式标准，无需 jose）。
历史 pbkdf2$ 哈希仍可验证（存量账号平滑过渡），新哈希一律 bcrypt。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import bcrypt


class PasswordHasher:
    """密码哈希。72-byte 上限来自 bcrypt 截断限制（spec §4 users）。"""

    MAX_PASSWORD_BYTES = 72
    ROUNDS = 12

    def hash(self, password: str) -> str:
        self._validate_length(password)
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=self.ROUNDS)).decode()

    def verify(self, password: str, hashed: str) -> bool:
        if len(password.encode()) > self.MAX_PASSWORD_BYTES:
            return False
        try:
            if hashed.startswith("pbkdf2$"):
                # 旧算法（MVP 骨架期产物）：按原参数校验，登录成功后下次修改密码自然升级
                _, rounds_s, salt, hex_digest = hashed.split("$")
                digest = hashlib.pbkdf2_hmac(
                    "sha256", password.encode(), salt.encode(), int(rounds_s)
                )
                return hmac.compare_digest(digest.hex(), hex_digest)
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except (ValueError, TypeError):
            return False

    def _validate_length(self, password: str) -> None:
        if len(password.encode()) > self.MAX_PASSWORD_BYTES:
            raise ValueError("password exceeds 72 bytes (bcrypt truncation limit)")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_token(payload: dict, secret: str, expires_in_seconds: int) -> str:
    body = dict(payload)
    now = int(time.time())
    body.update({"iat": now, "exp": now + expires_in_seconds})
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload_b64 = _b64url(json.dumps(body).encode())
    signing_input = f"{header}.{payload_b64}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload_b64}.{_b64url(sig)}"


def decode_token(token: str, secret: str) -> dict | None:
    """验签 + 解析；任何失败（分段/编码/JSON/字段类型/签名/过期）统一返回 None。

    issue #28：认证边界把一切畸形 token 视为「未认证」而非 500——
    异常不外传，响应不含 token/密钥/内部细节。
    """
    try:
        if not isinstance(token, str) or not isinstance(secret, str):
            return None
        header_b64, payload_b64, sig_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode()
        expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp", 0)
        # exp 非数值（字符串/列表等字段类型异常）同样视为无效 token
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) \
                or exp < time.time():
            return None
        return payload
    except Exception:  # noqa: BLE001 — 解析/解码失败一律视为未认证（issue #28）
        return None


class _MemoryRefreshStore:
    """进程内降级实现：重启即丢 rotation 状态，仅用于无 Redis 的开发/测试。"""

    def __init__(self) -> None:
        # family_id -> {token_jti: status}; status: active | revoked
        self._families: dict[str, dict[str, str]] = {}

    def issue(self, family_id: str, jti: str) -> None:
        self._families.setdefault(family_id, {})[jti] = "active"

    def rotate(self, family_id: str, old_jti: str, new_jti: str) -> bool:
        """成功返回 True；检测到 reuse 返回 False 并撤销整个 family。"""
        family = self._families.get(family_id)
        if family is None or old_jti not in family:
            return False
        if family[old_jti] == "revoked":
            for jti in list(family):
                family[jti] = "revoked"
            return False
        family[old_jti] = "revoked"
        family[new_jti] = "active"
        return True

    def is_active(self, family_id: str, jti: str) -> bool:
        return self._families.get(family_id, {}).get(jti) == "active"

    def revoke_family(self, family_id: str) -> None:
        for jti in self._families.get(family_id, {}):
            self._families[family_id][jti] = "revoked"


# 轮换三步（查状态/判 reuse/翻转变更）必须在服务端原子完成，
# 否则并发重放可把一个 refresh 换出两个有效 token（Lua 保证原子性）
_ROTATE_LUA = """
if redis.call('EXISTS', KEYS[1] .. ':dead') == 1 then return 0 end
local st = redis.call('HGET', KEYS[1], ARGV[1])
if not st then return 0 end
if st == 'revoked' then
  redis.call('SET', KEYS[1] .. ':dead', '1', 'EX', tonumber(ARGV[3]))
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], 'revoked')
redis.call('HSET', KEYS[1], ARGV[2], 'active')
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


class RefreshTokenStore:
    """Refresh rotation + reuse detection — spec §3 auth/refresh。

    Redis 可达则状态跨进程共享且带 TTL（多实例部署的正确形态）；
    不可达时降级进程内（开发/测试形态，行为与旧版一致）。降级判定不只
    在构造期：运行中掉线同样降级（对齐 task_queue 的冷却模式），冷却结束
    自动重试 Redis。降级窗口内 rotation 状态分裂为 Redis/内存两段——
    多实例部署以 Redis 为强制依赖（infra-spec），单实例形态可接受此取舍。
    """

    # 运行中掉线的重试冷却：期间直接走 memory，避免每请求阻塞在超时上
    _REDIS_COOLDOWN = 5.0

    def __init__(self, redis_url: str = "", ttl_seconds: int = 7 * 86400) -> None:
        self._ttl = ttl_seconds
        self._memory = _MemoryRefreshStore()
        self._redis = None
        self._down_until = 0.0  # monotonic 时间戳：冷却期内跳过 Redis 直接降级
        if redis_url:
            try:
                import redis

                client = redis.Redis.from_url(redis_url,
                                              socket_connect_timeout=1)
                client.ping()
                self._redis = client
            except Exception:  # noqa: BLE001 — 无 Redis 是合法部署形态
                self._redis = None

    def _redis_ok(self) -> bool:
        return self._redis is not None and time.monotonic() >= self._down_until

    def _mark_redis_down(self, exc: Exception) -> None:
        self._down_until = time.monotonic() + self._REDIS_COOLDOWN
        print(f"[auth] redis op failed ({exc.__class__.__name__}); "
              "refresh store degraded to in-process state")

    def _key(self, family_id: str) -> str:
        return f"rt:{family_id}"

    def issue(self, family_id: str, jti: str) -> None:
        if self._redis_ok():
            k = self._key(family_id)
            try:
                pipe = self._redis.pipeline()
                pipe.hset(k, jti, "active")
                pipe.expire(k, self._ttl)
                pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001 — 运行中掉线即降级
                self._mark_redis_down(exc)
        self._memory.issue(family_id, jti)

    def rotate(self, family_id: str, old_jti: str, new_jti: str) -> bool:
        if self._redis_ok():
            try:
                result = self._redis.eval(_ROTATE_LUA, 1, self._key(family_id),
                                          old_jti, new_jti, self._ttl)
                return bool(result)
            except Exception as exc:  # noqa: BLE001 — 运行中掉线即降级
                self._mark_redis_down(exc)
        return self._memory.rotate(family_id, old_jti, new_jti)

    def is_active(self, family_id: str, jti: str) -> bool:
        if self._redis_ok():
            k = self._key(family_id)
            try:
                if self._redis.exists(f"{k}:dead"):
                    return False
                return self._redis.hget(k, jti) == b"active"
            except Exception as exc:  # noqa: BLE001 — 运行中掉线即降级
                self._mark_redis_down(exc)
        return self._memory.is_active(family_id, jti)

    def revoke_family(self, family_id: str) -> None:
        if self._redis_ok():
            k = self._key(family_id)
            try:
                pipe = self._redis.pipeline()
                pipe.set(f"{k}:dead", "1", ex=self._ttl)   # 已发 token 一律拒
                pipe.delete(k)
                pipe.execute()
                return
            except Exception as exc:  # noqa: BLE001 — 运行中掉线即降级
                self._mark_redis_down(exc)
        self._memory.revoke_family(family_id)
