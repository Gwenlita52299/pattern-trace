"""JWT / 密码哈希 / refresh rotation — backend-api-spec §5."""
import time

import pytest

from backend.core.security import (
    PasswordHasher, RefreshTokenStore,
    create_token, decode_token,
)


class TestPasswordHasher:
    def setup_method(self):
        self.hasher = PasswordHasher()

    def test_hash_and_verify_correct_password(self):
        hashed = self.hasher.hash("S3cure!")
        assert self.hasher.verify("S3cure!", hashed)

    def test_verify_wrong_password_returns_false(self):
        hashed = self.hasher.hash("S3cure!")
        assert not self.hasher.verify("wrong", hashed)

    def test_hash_is_salted_each_time(self):
        h1 = self.hasher.hash("same")
        h2 = self.hasher.hash("same")
        assert h1 != h2

    def test_72_byte_limit_rejected_on_hash(self):
        long_pw = "a" * 73
        with pytest.raises(ValueError, match="72 bytes"):
            self.hasher.hash(long_pw)

    def test_verify_over_72_bytes_returns_false(self):
        hashed = self.hasher.hash("ok")
        assert not self.hasher.verify("a" * 73, hashed)

    def test_verify_malformed_hash_returns_false(self):
        assert not self.hasher.verify("pw", "not-a-valid-hash")

    def test_verify_empty_hash_returns_false(self):
        assert not self.hasher.verify("pw", "")


class TestJWT:
    SECRET = "unit-test-secret"

    def test_create_and_decode_roundtrip(self):
        token = create_token({"sub": "user@test"}, self.SECRET, 900)
        payload = decode_token(token, self.SECRET)
        assert payload is not None
        assert payload["sub"] == "user@test"
        assert payload["exp"] > int(time.time())

    def test_expired_token_returns_none(self):
        token = create_token({"sub": "u"}, self.SECRET, -10)
        assert decode_token(token, self.SECRET) is None

    def test_wrong_secret_returns_none(self):
        token = create_token({"sub": "u"}, self.SECRET, 900)
        assert decode_token(token, "other-secret") is None

    def test_malformed_token_no_dots_returns_none(self):
        assert decode_token("nodots", self.SECRET) is None

    def test_tampered_payload_returns_none(self):
        token = create_token({"sub": "u"}, self.SECRET, 900)
        header, payload, sig = token.split(".")
        tampered_payload = payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB")
        assert decode_token(f"{header}.{tampered_payload}.{sig}", self.SECRET) is None


class TestRefreshTokenStore:
    def setup_method(self):
        self.store = RefreshTokenStore()

    def test_issue_and_active(self):
        self.store.issue("fam1", "t1")
        assert self.store.is_active("fam1", "t1")

    def test_rotate_revokes_old_activates_new(self):
        self.store.issue("fam1", "t1")
        assert self.store.rotate("fam1", "t1", "t2") is True
        assert not self.store.is_active("fam1", "t1")
        assert self.store.is_active("fam1", "t2")

    def test_reuse_detection_revokes_entire_family(self):
        self.store.issue("fam1", "t1")
        self.store.rotate("fam1", "t1", "t2")
        # attacker replays old t1
        result = self.store.rotate("fam1", "t1", "t3")
        assert result is False  # reuse detected
        assert not self.store.is_active("fam1", "t2"), "entire family must be revoked"
        assert not self.store.is_active("fam1", "t3")

    def test_rotate_unknown_family_returns_false(self):
        assert self.store.rotate("unknown", "t1", "t2") is False

    def test_revoke_family(self):
        self.store.issue("fam", "a")
        self.store.issue("fam", "b")
        self.store.revoke_family("fam")
        assert not self.store.is_active("fam", "a")
        assert not self.store.is_active("fam", "b")

    def test_revoke_unknown_family_no_error(self):
        self.store.revoke_family("nonexistent")
