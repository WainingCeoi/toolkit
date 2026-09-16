"""AES-256-CBC for the BitComet login; pure Python where cryptography has no wheel."""

from __future__ import annotations

KEY_LEN = 32  # AES-256 only; the envelope's PBKDF2 keys are always dklen=32
BLOCK = 16


# --- GF(2^8) AND THE S-BOX ---
# Computed, not transcribed: one mistyped S-box literal still round-trips.
def _xtime(a: int) -> int:
    """Multiply by x (i.e. by 2) in GF(2^8) modulo AES's polynomial 0x11B."""
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _build_sboxes() -> tuple[bytes, bytes]:
    # inv(a) = 3^(255 - log3(a)) over the cyclic group of order 255.
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


# --- THE BLOCK CIPHER ---
# State is flat, column-major (byte i: row i%4, column i//4); ShiftRows permutes it.
_SHIFT_ROWS = [r + 4 * ((c + r) % 4) for c in range(4) for r in range(4)]
_INV_SHIFT_ROWS = [r + 4 * ((c - r) % 4) for c in range(4) for r in range(4)]

# First row of each circulant matrix; output row r, input row k uses row[(k - r) % 4].
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
            # The extra SubWord is what makes the 256-bit schedule differ.
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


# --- CBC ---
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


# --- BACKEND CHOICE ---
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # Intel Mac: no wheel, sdist needs Rust; pyproject skips it
    encrypt_cbc = _encrypt_cbc_py
    decrypt_cbc = _decrypt_cbc_py
else:

    def encrypt_cbc(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(plaintext) + encryptor.finalize()

    def decrypt_cbc(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return decryptor.update(ciphertext) + decryptor.finalize()
