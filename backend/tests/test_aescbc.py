"""The AES-256-CBC pair behind the login envelope, held to the NIST vectors.

The pure-Python fallback is what an Intel Mac runs (no `cryptography` wheel
there -- see aescbc.py), so it is tested directly and unconditionally, even on
machines where the public pair delegates to `cryptography`: the platform that
NEEDS the fallback is exactly the one this suite is least often run on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from toolkit_engine.aescbc import (
    _decrypt_cbc_py,
    _encrypt_cbc_py,
    decrypt_cbc,
    encrypt_cbc,
)

# NIST SP 800-38A appendix F.2.5/F.2.6 (CBC-AES256), the standard's own
# worked example: one key, one IV, four chained blocks.
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


def test_pure_decrypt_matches_the_nist_cbc_vectors():
    assert _decrypt_cbc_py(KEY, IV, CIPHERTEXT) == PLAINTEXT


def test_pure_single_block_matches_fips_197():
    # FIPS-197 appendix C.3, the bare AES-256 block example. A zero IV makes
    # CBC of one block the bare cipher, so the same pair pins the block core
    # independently of the chaining.
    key = bytes.fromhex(
        "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    )
    plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")

    assert _encrypt_cbc_py(key, bytes(16), plaintext) == expected
    assert _decrypt_cbc_py(key, bytes(16), expected) == plaintext


def test_pure_and_cryptography_agree_both_ways():
    # Byte-equality against the audited implementation, wherever it is
    # installed. On an Intel Mac the public pair IS the pure pair and this
    # would prove nothing, so it skips; the vector tests carry the proof there.
    pytest.importorskip("cryptography")
    for blocks in (1, 2, 7):
        key, iv, data = os.urandom(32), os.urandom(16), os.urandom(16 * blocks)
        sealed = encrypt_cbc(key, iv, data)

        assert _encrypt_cbc_py(key, iv, data) == sealed
        assert _decrypt_cbc_py(key, iv, sealed) == data
        assert decrypt_cbc(key, iv, _encrypt_cbc_py(key, iv, data)) == data


def test_pure_pair_refuses_the_sizes_the_envelope_never_sends():
    key, iv, block = bytes(32), bytes(16), bytes(16)

    with pytest.raises(ValueError, match="key"):
        _encrypt_cbc_py(bytes(16), iv, block)  # AES-128 is a caller bug here
    with pytest.raises(ValueError, match="IV"):
        _encrypt_cbc_py(key, bytes(8), block)
    with pytest.raises(ValueError, match="multiple"):
        _decrypt_cbc_py(key, iv, bytes(15))


def test_login_envelope_survives_on_the_pure_pair(monkeypatch):
    # What an Intel Mac actually runs: the whole envelope -- PBKDF2, PKCS7,
    # HMAC and all -- with the pure pair underneath instead of cryptography.
    from toolkit_engine import bitcomet

    monkeypatch.setattr(bitcomet, "encrypt_cbc", _encrypt_cbc_py)
    monkeypatch.setattr(bitcomet, "decrypt_cbc", _decrypt_cbc_py)

    client_id = "6f2c48f5-0000-4000-8000-badc0ffee000"
    plaintext = '{"username": "someone", "password": "hünter2"}'
    assert bitcomet.decrypt(bitcomet.encrypt(plaintext, client_id), client_id) == (
        plaintext
    )


def test_the_import_fallback_actually_wires_in_the_pure_pair():
    # The monkeypatch test above proves the pure pair CAN carry the envelope;
    # this one proves the `except ImportError` in aescbc.py actually selects
    # it. A subprocess, because the wiring under test runs once, at first
    # import -- and in this process cryptography is already imported and
    # cached, so no in-process trick can make that import fail honestly.
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name.partition(".")[0] == "cryptography":
                    raise ImportError("blocked: simulating an Intel Mac")

        sys.meta_path.insert(0, Blocker())
        from toolkit_engine import aescbc, bitcomet

        assert aescbc.encrypt_cbc is aescbc._encrypt_cbc_py
        assert aescbc.decrypt_cbc is aescbc._decrypt_cbc_py
        blob = bitcomet.encrypt("the credentials", "the-client-id")
        assert bitcomet.decrypt(blob, "the-client-id") == "the credentials"
        """
    )
    subprocess.run([sys.executable, "-c", script], check=True)
