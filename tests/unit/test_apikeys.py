"""API key format (B-20: threat S-02)."""

from __future__ import annotations

import hashlib

from rai.core import apikeys


def test_a_new_key_is_random_and_only_its_hash_is_kept() -> None:
    key = apikeys.generate()
    other = apikeys.generate()
    assert key.token != other.token
    assert key.prefix != other.prefix
    parsed = apikeys.parse(key.token)
    assert parsed is not None
    prefix, secret = parsed
    assert prefix == key.prefix
    assert len(prefix) == 12
    assert len(secret) == 43  # 32 random bytes
    assert key.secret_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert key.secret_hash not in key.token
    assert key.token not in repr(key)
    assert apikeys.matches(secret, key.secret_hash)
    assert not apikeys.matches(secret[:-1] + ("A" if secret[-1] != "A" else "B"), key.secret_hash)


def test_only_well_formed_keys_parse() -> None:
    token = apikeys.generate().token
    assert apikeys.is_key(token)
    assert not apikeys.is_key("eyJhbGciOiJIUzI1NiJ9.e30.x")  # a JWT
    assert not apikeys.is_key("rairt_refresh-token")  # a refresh token
    for bad in (token + "x", token[:-1], token.replace("rai_", "rai-", 1), "rai_", "rai_abc_def",
                token.upper()):  # fmt: skip
        assert apikeys.parse(bad) is None, bad
