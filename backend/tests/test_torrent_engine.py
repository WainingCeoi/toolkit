"""Torrent Downloader: pure engine units (bencode, magnet, filter)."""

from __future__ import annotations

import base64
import hashlib

import pytest

from toolkit_engine.filetypes import SIZED_CATEGORIES, categorize
from toolkit_engine.torrent import (
    TorrentFile,
    bdecode,
    bencode,
    format_selection,
    parse_magnet,
    parse_torrent,
    select_files,
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Movie.2024.1080p.mkv", "video"),
        ("nested/dir/clip.MP4", "video"),
        ("soundtrack.flac", "audio"),
        ("cover.jpg", "image"),
        ("Movie.2024.chi.srt", "subtitle"),
        ("readme.pdf", "document"),
        ("extras.rar", "archive"),
        ("RARBG.txt", "document"),
        ("no_extension", "other"),
        ("weird.xyz", "other"),
    ],
)
def test_categorize_maps_extensions_to_categories(path, expected):
    assert categorize(path) == expected


def test_only_video_and_audio_are_size_gated():
    # The whole point of the filter design: a 100MB floor must never be able
    # to discard a subtitle, which is ~40KB and could never pass it.
    assert SIZED_CATEGORIES == frozenset({"video", "audio"})


# =======================================================
# BENCODE / .TORRENT
# =======================================================
def make_torrent(files, name="Example.Release"):
    """Build a real multi-file .torrent as bytes, so tests need no fixtures."""
    return bencode(
        {
            b"announce": b"udp://tracker.example:80",
            b"info": {
                b"name": name.encode(),
                b"piece length": 262144,
                b"pieces": b"\x00" * 20,
                b"files": [
                    {b"length": size, b"path": [p.encode() for p in path.split("/")]}
                    for path, size in files
                ],
            },
        }
    )


def test_bencode_round_trips_every_type():
    value = {b"a": 1, b"b": [b"x", 2], b"c": {b"d": b"e"}}
    assert bdecode(bencode(value)) == value


def test_bencode_encodes_negative_and_zero_integers():
    assert bdecode(bencode({b"n": -5, b"z": 0})) == {b"n": -5, b"z": 0}


def test_parse_torrent_lists_multi_file_contents():
    data = make_torrent(
        [("Movie.mkv", 2_000_000_000), ("Sample/sample.mkv", 40_000_000)]
    )
    info = parse_torrent(data)

    assert info.name == "Example.Release"
    assert info.total_bytes == 2_040_000_000
    assert info.files == [
        TorrentFile(index=1, path="Movie.mkv", size=2_000_000_000),
        TorrentFile(index=2, path="Sample/sample.mkv", size=40_000_000),
    ]


def test_parse_torrent_handles_single_file_mode():
    data = bencode(
        {
            b"info": {
                b"name": b"Solo.mkv",
                b"length": 900_000_000,
                b"piece length": 262144,
                b"pieces": b"\x00" * 20,
            }
        }
    )
    info = parse_torrent(data)
    assert info.files == [TorrentFile(index=1, path="Solo.mkv", size=900_000_000)]


def test_parse_torrent_computes_infohash_over_raw_info_bytes():
    data = make_torrent([("A.mkv", 10)])
    # `info` sorts last, so its raw span runs to the outer dict's closing 'e'.
    start = data.index(b"4:info") + len(b"4:info")
    expected = hashlib.sha1(data[start:-1], usedforsecurity=False).hexdigest()

    assert parse_torrent(data).infohash == expected


def test_parse_torrent_rejects_a_file_with_no_info_dict():
    with pytest.raises(ValueError, match="no info dict"):
        parse_torrent(bencode({b"announce": b"udp://x"}))


def test_parse_torrent_rejects_junk():
    with pytest.raises(ValueError, match="not a bencoded"):
        parse_torrent(b"this is not a torrent")


# =======================================================
# MAGNET
# =======================================================
HASH40 = "c9e15763f722f23e98a29decdfae341b98d53056"


def test_parse_magnet_reads_a_hex_btih():
    infohash, name = parse_magnet(f"magnet:?xt=urn:btih:{HASH40}&dn=Some.Name")
    assert infohash == HASH40
    assert name == "Some.Name"


def test_parse_magnet_lowercases_and_survives_a_missing_name():
    infohash, name = parse_magnet(f"magnet:?xt=urn:btih:{HASH40.upper()}")
    assert infohash == HASH40
    assert name is None


def test_parse_magnet_decodes_a_base32_btih():
    # 32-char base32 magnets are common in the wild and must not be rejected.
    b32 = base64.b32encode(bytes.fromhex(HASH40)).decode()
    assert parse_magnet(f"magnet:?xt=urn:btih:{b32}")[0] == HASH40


@pytest.mark.parametrize(
    "uri",
    ["http://example.com/x.torrent", "magnet:?dn=NoHash", "magnet:?xt=urn:sha1:abc"],
)
def test_parse_magnet_rejects_non_magnets_and_hashless_magnets(uri):
    with pytest.raises(ValueError):
        parse_magnet(uri)


# =======================================================
# SELECTION
# =======================================================
SAMPLE = [
    TorrentFile(index=1, path="Movie.2024.1080p.mkv", size=2_000_000_000),
    TorrentFile(index=2, path="Sample/sample.mkv", size=40_000_000),
    TorrentFile(index=3, path="Movie.2024.chi.srt", size=45_000),
    TorrentFile(index=4, path="Screens/01.jpg", size=300_000),
    TorrentFile(index=5, path="RARBG.txt", size=30),
]


def test_select_files_defaults_to_large_videos_only():
    assert select_files(SAMPLE, {"video"}, 100 * 1024 * 1024) == [1]


def test_size_floor_does_not_apply_to_subtitles():
    # THE case this filter design exists for: the 100MB floor gates the video
    # but must let the 45KB subtitle through.
    got = select_files(SAMPLE, {"video", "subtitle"}, 100 * 1024 * 1024)
    assert got == [1, 3]


def test_a_zero_floor_keeps_every_file_in_the_chosen_categories():
    assert select_files(SAMPLE, {"video"}, 0) == [1, 2]


def test_select_files_returns_empty_when_nothing_matches():
    assert select_files(SAMPLE, {"archive"}, 0) == []


def test_select_files_preserves_ascending_index_order():
    got = select_files(SAMPLE, {"video", "image", "subtitle", "document"}, 0)
    assert got == sorted(got)


def test_format_selection_sorts_and_joins_the_indexes():
    assert format_selection([3, 1, 2]) == "1,2,3"


def test_format_selection_rejects_an_empty_selection():
    # A torrent with every file deselected downloads nothing and calls itself
    # finished, so the empty case is refused instead of being committed.
    with pytest.raises(ValueError, match="at least one file"):
        format_selection([])
