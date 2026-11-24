"""AES-256-CBC encryption and its pure-Python fallback against known vectors."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from toolkit_engine.aescbc import _encrypt_cbc_py, encrypt_cbc

# NIST SP 800-38A appendix F.2.5 (CBC-AES256).
KEY = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
PLAINTEXT = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)
CIPHERTEXT = bytes.fromhex(
    "f58c4c04d6e5f1ba779eabfb5f7bfbd6"
    "9cfc4e967edb808d679f777bc6702c7d"
    "39f23369a9d9bacfa530e26304231461"
    "b2eb05e2c39be9fcda6c19078c6a9d1b"
)


def test_pure_encrypt_matches_the_nist_cbc_vectors():
    assert _encrypt_cbc_py(KEY, IV, PLAINTEXT) == CIPHERTEXT


def test_pure_single_block_matches_fips_197():
    # FIPS-197 appendix C.3; a zero IV makes one CBC block the bare cipher.
    key = bytes.fromhex(
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    )
    plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")

    assert _encrypt_cbc_py(key, bytes(16), plaintext) == expected


def test_pure_and_cryptography_produce_the_same_ciphertext():
    pytest.importorskip("cryptography")
    for blocks in (1, 2, 7):
        key, iv, data = os.urandom(32), os.urandom(16), os.urandom(16 * blocks)
        sealed = encrypt_cbc(key, iv, data)

        assert _encrypt_cbc_py(key, iv, data) == sealed


def test_pure_encryption_rejects_invalid_key_iv_and_block_lengths():
    key, iv, block = bytes(32), bytes(16), bytes(16)

    with pytest.raises(ValueError, match="key"):
        _encrypt_cbc_py(bytes(16), iv, block)  # AES-128 is a caller bug here
    with pytest.raises(ValueError, match="IV"):
        _encrypt_cbc_py(key, bytes(8), block)
    with pytest.raises(ValueError, match="multiple"):
        _encrypt_cbc_py(key, iv, bytes(15))


def test_login_envelope_matches_a_known_vector_without_cryptography():
    # A subprocess: the fallback wiring runs once, at first import.
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys
        from unittest.mock import patch

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name.partition(".")[0] == "cryptography":
                    raise ImportError("blocked: simulating an Intel Mac")

        sys.meta_path.insert(0, Blocker())
        from toolkit_engine import bitcomet

        with patch.object(bitcomet.os, "urandom", side_effect=(
            bytes(range(8)), bytes(range(8, 16)), bytes(range(16, 32))
        )):
            blob = bitcomet.encrypt("the credentials", "the-client-id")
        # Generated independently using a known salt and IV and PyCA AES-256-CBC.
        assert blob == (
            "AwEAAQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eH8EM9ByZTXaXaDxIGR"
            "+zlqdDcmd1i2GWkYpL5b3sb7hV0aHDuo11KHVVD4Iykm9cFA=="
        )
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True)
