"""Password hashing and JWT encode/decode."""

from datetime import timedelta

import pytest
from jose import JWTError

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_is_not_the_plaintext(self):
        assert hash_password("hunter2") != "hunter2"

    def test_correct_password_verifies(self):
        assert verify_password("hunter2", hash_password("hunter2"))

    def test_wrong_password_does_not_verify(self):
        assert not verify_password("wrong", hash_password("hunter2"))

    def test_hashes_are_salted(self):
        # Same input, different output - otherwise identical passwords would be
        # identifiable from the stored hashes alone.
        assert hash_password("hunter2") != hash_password("hunter2")

    def test_case_sensitive(self):
        assert not verify_password("Hunter2", hash_password("hunter2"))

    def test_long_password_is_accepted(self):
        # bcrypt truncates at 72 bytes; this asserts it does not raise.
        secret = "x" * 200
        assert verify_password(secret, hash_password(secret))


class TestAccessTokens:
    def test_round_trips_the_subject(self):
        token = create_access_token({"sub": "user@example.com"})
        assert decode_access_token(token)["sub"] == "user@example.com"

    def test_carries_an_expiry(self):
        assert "exp" in decode_access_token(create_access_token({"sub": "a@b.c"}))

    def test_expired_token_is_rejected(self):
        token = create_access_token({"sub": "a@b.c"}, expires_delta=timedelta(seconds=-10))
        with pytest.raises(JWTError):
            decode_access_token(token)

    def test_tampered_token_is_rejected(self):
        token = create_access_token({"sub": "a@b.c"})
        # Corrupt the payload segment; the signature no longer matches.
        head, payload, sig = token.split(".")
        with pytest.raises(JWTError):
            decode_access_token(f"{head}.{payload[:-4]}XXXX.{sig}")

    def test_garbage_is_rejected(self):
        with pytest.raises(JWTError):
            decode_access_token("not-a-token")

    def test_token_signed_with_another_secret_is_rejected(self):
        from jose import jwt

        from app.config import settings

        forged = jwt.encode(
            {"sub": "attacker@example.com"},
            "some-other-secret",
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(JWTError):
            decode_access_token(forged)
