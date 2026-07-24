"""Torrent Downloader engine: pure parsing and selection. No I/O, no network.

Everything here is a function of its arguments, which is what makes the tool
testable without aria2 installed or a swarm reachable: a .torrent is bencode,
so its file list is readable offline, and the category/size filter is a fold
over that list.
"""

from __future__ import annotations

import base64
import hashlib
import urllib.parse
from dataclasses import dataclass

from toolkit_engine.filetypes import SIZED_CATEGORIES, categorize


@dataclass(frozen=True)
class TorrentFile:
    index: int  # 1-based -- aria2's select-file numbering, used verbatim
    path: str  # path inside the torrent, '/'-joined
    size: int  # bytes


@dataclass(frozen=True)
class TorrentInfo:
    infohash: str  # lowercase hex btih
    name: str
    files: list[TorrentFile]
    total_bytes: int


# =======================================================
# BENCODE
# =======================================================
def bencode(value) -> bytes:
    """Encode dict/list/int/bytes to bencode. Dict keys are sorted, per spec."""
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted(value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(f"cannot bencode {type(value).__name__}")


def _decode(data: bytes, i: int) -> tuple[object, int]:
    """Decode one value at data[i:]. Returns (value, index_just_past_it)."""
    head = data[i : i + 1]
    if head == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1 : end]), end + 1
    if head == b"l":
        out: list = []
        i += 1
        while data[i : i + 1] != b"e":
            item, i = _decode(data, i)
            out.append(item)
        return out, i + 1
    if head == b"d":
        out_dict: dict = {}
        i += 1
        while data[i : i + 1] != b"e":
            key, i = _decode(data, i)
            val, i = _decode(data, i)
            out_dict[key] = val
        return out_dict, i + 1
    if head.isdigit():
        colon = data.index(b":", i)
        length = int(data[i:colon])
        start = colon + 1
        return data[start : start + length], start + length
    raise ValueError(f"invalid bencode at byte {i}")


def bdecode(data: bytes):
    """Decode a complete bencoded document."""
    value, _ = _decode(data, 0)
    return value


def _decode_root(data: bytes) -> tuple[dict, dict[bytes, tuple[int, int]]]:
    """Decode the outer dict, recording each value's raw byte span.

    The span is what makes the infohash correct: btih is SHA1 over the ORIGINAL
    bencoded `info` bytes, and re-encoding a decoded dict can differ from what
    the file actually contained (key order, integer forms). Slicing the source
    sidesteps that entirely.
    """
    if data[0:1] != b"d":
        raise ValueError("not a bencoded dictionary")
    out: dict = {}
    spans: dict[bytes, tuple[int, int]] = {}
    i = 1
    while data[i : i + 1] != b"e":
        key, i = _decode(data, i)
        start = i
        value, i = _decode(data, i)
        out[key] = value
        spans[key] = (start, i)
    return out, spans


# =======================================================
# .TORRENT
# =======================================================
def parse_torrent(data: bytes) -> TorrentInfo:
    """Read a .torrent's file list and infohash. Offline, no network."""
    try:
        meta, spans = _decode_root(data)
    except ValueError:
        raise
    except (IndexError, KeyError) as exc:  # truncated / malformed
        raise ValueError("not a bencoded dictionary") from exc

    if b"info" not in meta:
        raise ValueError("torrent has no info dict")

    start, end = spans[b"info"]
    # btih is SHA1 by the BitTorrent spec -- not a security choice.
    infohash = hashlib.sha1(data[start:end], usedforsecurity=False).hexdigest()

    info = meta[b"info"]
    name = info[b"name"].decode("utf-8", "replace")

    if b"files" in info:
        files = [
            TorrentFile(
                index=n,
                path="/".join(
                    part.decode("utf-8", "replace") for part in entry[b"path"]
                ),
                size=int(entry[b"length"]),
            )
            for n, entry in enumerate(info[b"files"], start=1)
        ]
    else:
        files = [TorrentFile(index=1, path=name, size=int(info[b"length"]))]

    return TorrentInfo(
        infohash=infohash,
        name=name,
        files=files,
        total_bytes=sum(f.size for f in files),
    )


# =======================================================
# MAGNET
# =======================================================
def parse_magnet(uri: str) -> tuple[str, str | None]:
    """Return (infohash, display_name) from a magnet URI.

    Accepts both btih forms: 40-char hex and 32-char base32.
    """
    if not uri.startswith("magnet:?"):
        raise ValueError("not a magnet link")

    params = urllib.parse.parse_qs(uri[len("magnet:?") :])
    for topic in params.get("xt", []):
        if not topic.startswith("urn:btih:"):
            continue
        raw = topic[len("urn:btih:") :]
        if len(raw) == 40:
            infohash = raw.lower()
        elif len(raw) == 32:
            infohash = base64.b32decode(raw.upper()).hex()
        else:
            raise ValueError(f"unrecognised btih length: {len(raw)}")
        display = params.get("dn", [None])[0]
        return infohash, display

    raise ValueError("magnet link has no btih hash")


# =======================================================
# SELECTION
# =======================================================
def select_files(
    files: list[TorrentFile], categories: set[str], min_bytes: int
) -> list[int]:
    """1-based indices to download: in a chosen category, and big enough.

    The size floor applies only to SIZED_CATEGORIES. See filetypes.py for why.
    """
    chosen = set(categories)
    selected = []
    for entry in files:
        category = categorize(entry.path)
        if category not in chosen:
            continue
        if category in SIZED_CATEGORIES and entry.size < min_bytes:
            continue
        selected.append(entry.index)
    return selected


def format_selection(indices: list[int]) -> str:
    """Render indices as aria2's select-file value, e.g. "1,4,7".

    Never returns "": aria2 treats an empty select-file as a silent no-op, and
    a torrent with every file deselected flips straight to complete having
    downloaded nothing.
    """
    if not indices:
        raise ValueError("a torrent must download at least one file")
    return ",".join(str(i) for i in sorted(indices))
