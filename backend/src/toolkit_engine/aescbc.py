"""AES-256-CBC for the BitComet login envelope, with or without `cryptography`.

The envelope (see bitcomet.py) needs exactly one primitive the stdlib does not
carry: a block cipher. `cryptography` provides it everywhere it can be
installed -- but its wheels stopped covering macOS x86_64, so on an Intel Mac
the install falls back to building the sdist, and that build demands a Rust
toolchain. A compiler ecosystem, installed for one cipher call per login, is
the wrong trade on a machine that only wants to run this app -- so on that one
platform pyproject.toml skips the dependency (see the marker there) and this
module supplies the cipher itself.

Hand-rolling AES is normally the cardinal sin of crypto engineering, so the
reasons it is acceptable HERE are spelled out:

* The envelope is obfuscation, not transport security. The "password" its keys
  derive from is the client_id -- a UUID the client invents and sends IN THE
  CLEAR next to the ciphertext, over plain HTTP. There is no secret for a
  timing side channel to leak that the wire does not already carry.
* The traffic is one ~100-byte blob per login, so pure-Python speed is
  irrelevant.
* Correctness is pinned, not assumed: test_aescbc.py holds this implementation
  to the published NIST vectors (FIPS-197 C.3, SP 800-38A F.2.5/F.2.6) and to
  byte-equality against `cryptography`, both ways, wherever that is installed.

When `cryptography` is importable the public pair at the bottom delegates to
it, so every platform with a wheel runs the audited C implementation and the
fallback is exercised only where the alternative is "install Rust first".

Only AES-256 is implemented: both envelope keys are PBKDF2 with dklen=32, so a
16- or 24-byte key arriving here is a caller bug worth refusing, not a size
worth supporting.
"""

from __future__ import annotations

KEY_LEN = 32  # AES-256 only; the envelope's PBKDF2 keys are always dklen=32
BLOCK = 16


# =======================================================
# GF(2^8) AND THE S-BOX
# =======================================================
# The tables are COMPUTED, not transcribed. The S-box is 256 hex literals in
# print, and one mistyped literal yields a cipher that still round-trips with
# itself while agreeing with no other AES on earth. Deriving the tables from
# the field arithmetic that defines them (FIPS-197 5.1.1) leaves nothing to
# mistype, and the NIST-vector tests would catch the derivation being wrong.
def _xtime(a: int) -> int:
    """Multiply by x (i.e. by 2) in GF(2^8) modulo AES's polynomial 0x11B."""
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _build_sboxes() -> tuple[bytes, bytes]:
    # exp/log tables over the generator 3 give every multiplicative inverse:
    # inv(a) = 3^(255 - log3(a)), since the nonzero elements form a cyclic
    # group of order 255.
    exp, log = [0] * 256, [0] * 256
    value = 1
    for power in range(255):
        exp[power] = value
        log[value] = power
        value ^= _xtime(value)  # times 3, i.e. times x+1

    sbox = bytearray(256)
    for byte in range(256):
        inverse = exp[(255 - log[byte]) % 255] if byte else 0
        # FIPS-197 5.1.1's affine transform, bit by bit.
        substituted = 0
        for i in range(8):
            bit = (
                (inverse >> i)
                ^ (inverse >> ((i + 4) % 8))
                ^ (inverse >> ((i + 5) % 8))
                ^ (inverse >> ((i + 6) % 8))
                ^ (inverse >> ((i + 7) % 8))
                ^ (0x63 >> i)
            ) & 1
            substituted |= bit << i
        sbox[byte] = substituted

    inv_sbox = bytearray(256)
    for byte, substituted in enumerate(sbox):
        inv_sbox[substituted] = byte
    return bytes(sbox), bytes(inv_sbox)


_SBOX, _INV_SBOX = _build_sboxes()


def _mul(a: int, factor: int) -> int:
    """Multiply in GF(2^8); `factor` is one of MixColumns' small constants."""
    product = 0
    while factor:
        if factor & 1:
            product ^= a
        a = _xtime(a)
        factor >>= 1
    return product


# =======================================================
# THE BLOCK CIPHER
# =======================================================
# The 16-byte state is kept flat, in wire order. FIPS-197 fills its 4x4 grid
# column by column, so in this form byte i is row i%4 of column i//4 -- each
# consecutive 4-byte slice is one COLUMN, and ShiftRows becomes a fixed
# permutation of flat indexes, precomputed here once.
_SHIFT_ROWS = [r + 4 * ((c + r) % 4) for c in range(4) for r in range(4)]
_INV_SHIFT_ROWS = [r + 4 * ((c - r) % 4) for c in range(4) for r in range(4)]

# MixColumns and its inverse as the first row of each circulant matrix; the
# coefficient for output row r and input row k is row[(k - r) % 4].
_MIX = (2, 3, 1, 1)
_INV_MIX = (14, 11, 13, 9)


def _mix_columns(state: list[int], row: tuple[int, int, int, int]) -> list[int]:
    mixed = [0] * 16
    for col in range(0, 16, 4):
        for r in range(4):
            mixed[col + r] = (
                _mul(state[col], row[(0 - r) % 4])
                ^ _mul(state[col + 1], row[(1 - r) % 4])
                ^ _mul(state[col + 2], row[(2 - r) % 4])
                ^ _mul(state[col + 3], row[(3 - r) % 4])
            )
    return mixed


def _round_keys(key: bytes) -> list[bytes]:
    """The 15 round keys of AES-256 (FIPS-197 5.2: Nk=8, Nr=14)."""
    words = [key[i : i + 4] for i in range(0, KEY_LEN, 4)]
    rcon = 1
    for i in range(8, 60):
        temp = words[i - 1]
        if i % 8 == 0:
            temp = bytes(_SBOX[b] for b in temp[1:] + temp[:1])  # Rot then Sub
            temp = bytes((temp[0] ^ rcon,)) + temp[1:]
            rcon = _xtime(rcon)
        elif i % 8 == 4:
            # The extra SubWord mid-key is what distinguishes the 256-bit
            # schedule from the shorter ones.
            temp = bytes(_SBOX[b] for b in temp)
        words.append(bytes(a ^ b for a, b in zip(words[i - 8], temp, strict=True)))
    return [b"".join(words[i : i + 4]) for i in range(0, 60, 4)]


def _encrypt_block(block: bytes, keys: list[bytes]) -> bytes:
    state = [b ^ k for b, k in zip(block, keys[0], strict=True)]
    for key in keys[1:-1]:
        state = [_SBOX[b] for b in state]
        state = [state[i] for i in _SHIFT_ROWS]
        state = _mix_columns(state, _MIX)
        state = [b ^ k for b, k in zip(state, key, strict=True)]
    state = [_SBOX[b] for b in state]
    state = [state[i] for i in _SHIFT_ROWS]
    return bytes(b ^ k for b, k in zip(state, keys[-1], strict=True))


def _decrypt_block(block: bytes, keys: list[bytes]) -> bytes:
    state = [b ^ k for b, k in zip(block, keys[-1], strict=True)]
    state = [state[i] for i in _INV_SHIFT_ROWS]
    state = [_INV_SBOX[b] for b in state]
    for key in reversed(keys[1:-1]):
        state = [b ^ k for b, k in zip(state, key, strict=True)]
        state = _mix_columns(state, _INV_MIX)
        state = [state[i] for i in _INV_SHIFT_ROWS]
        state = [_INV_SBOX[b] for b in state]
    return bytes(b ^ k for b, k in zip(state, keys[0], strict=True))


# =======================================================
# CBC
# =======================================================
def _check(key: bytes, iv: bytes, data: bytes) -> None:
    if len(key) != KEY_LEN:
        raise ValueError(f"AES-256 needs a {KEY_LEN}-byte key, got {len(key)}")
    if len(iv) != BLOCK:
        raise ValueError(f"CBC needs a {BLOCK}-byte IV, got {len(iv)}")
    if len(data) % BLOCK:
        raise ValueError(f"data must be a multiple of {BLOCK} bytes, got {len(data)}")


def _encrypt_cbc_py(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Pure-Python AES-256-CBC. No padding: the envelope PKCS7-pads first."""
    _check(key, iv, plaintext)
    keys = _round_keys(key)
    out, previous = bytearray(), iv
    for i in range(0, len(plaintext), BLOCK):
        block = plaintext[i : i + BLOCK]
        previous = _encrypt_block(
            bytes(a ^ b for a, b in zip(block, previous, strict=True)), keys
        )
        out += previous
    return bytes(out)


def _decrypt_cbc_py(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Inverse of _encrypt_cbc_py; padding is likewise the caller's problem."""
    _check(key, iv, ciphertext)
    keys = _round_keys(key)
    out, previous = bytearray(), iv
    for i in range(0, len(ciphertext), BLOCK):
        block = ciphertext[i : i + BLOCK]
        decrypted = _decrypt_block(block, keys)
        out += bytes(a ^ b for a, b in zip(decrypted, previous, strict=True))
        previous = block
    return bytes(out)


# =======================================================
# BACKEND CHOICE
# =======================================================
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # an Intel Mac: see the module docstring and pyproject.toml
    encrypt_cbc = _encrypt_cbc_py
    decrypt_cbc = _decrypt_cbc_py
else:

    def encrypt_cbc(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(plaintext) + encryptor.finalize()

    def decrypt_cbc(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()
