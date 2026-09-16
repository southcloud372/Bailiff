import jwt
import pytest

from agent_platform.gateway.auth import AuthenticationError, TokenVerifier, encode_token
from agent_platform.gateway.policy import Principal

SECRET = "test-secret-key-for-unit-tests-0123456789"


def test_roundtrip() -> None:
    token = encode_token("u1", {"reader"}, SECRET)
    assert TokenVerifier(SECRET).verify(token) == Principal(
        id="u1", roles=frozenset({"reader"})
    )


def test_wrong_secret_rejected() -> None:
    token = encode_token("u1", {"reader"}, SECRET)
    with pytest.raises(AuthenticationError):
        TokenVerifier("wrong-secret-key-for-unit-tests-0123456789").verify(token)


def test_expired_token_rejected() -> None:
    token = encode_token("u1", {"reader"}, SECRET, ttl_seconds=-1)
    with pytest.raises(AuthenticationError):
        TokenVerifier(SECRET).verify(token)


def test_corrupted_signature_rejected() -> None:
    token = encode_token("u1", {"reader"}, SECRET)
    corrupted = token[:-1] + ("a" if token[-1] != "a" else "b")
    with pytest.raises(AuthenticationError):
        TokenVerifier(SECRET).verify(corrupted)


def test_missing_sub_rejected() -> None:
    token = jwt.encode({"roles": ["reader"], "exp": 9999999999}, SECRET, algorithm="HS256")
    with pytest.raises(AuthenticationError):
        TokenVerifier(SECRET).verify(token)
