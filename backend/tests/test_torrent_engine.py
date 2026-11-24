"""Torrent Downloader: torrent and magnet parsing and BitComet selection syntax."""

from __future__ import annotations

import base64
import hashlib

import pytest
from fake_bitcomet import bencode, make_torrent

from toolkit_engine.filetypes import categorize
from toolkit_engine.torrent import (
    TorrentFile,
    format_selection,
    parse_magnet,
    parse_torrent,
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


def test_parse_torrent_lists_multi_file_contents():
    data = make_torrent(
        [("Movie.mkv", 2_000_000_000), ("Sample/sample.mkv", 40_000_000)]
    )
    info = parse_torrent(data)

    assert info.name == "Example.Release"
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


@pytest.mark.parametrize(
    "info",
    [
        b"not-a-dict",
        {b"length": 5},  # no name
        {b"name": b"a"},  # neither files nor length
        {b"name": b"a", b"files": [{b"length": 1}]},  # a file entry with no path
        {b"name": 5, b"length": 1},  # a name that is not bytes
    ],
)
def test_parse_torrent_rejects_a_malformed_info_dict(info):
    # Valid bencode, wrong shape: the router turns ValueError into a 400, not a 500.
    with pytest.raises(ValueError, match="malformed"):
        parse_torrent(bencode({b"info": info}))


# --- MAGNET ---
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
    b32 = base64.b32encode(bytes.fromhex(HASH40)).decode()
    assert parse_magnet(f"magnet:?xt=urn:btih:{b32}")[0] == HASH40


@pytest.mark.parametrize(
    "uri",
    ["http://example.com/x.torrent", "magnet:?dn=NoHash", "magnet:?xt=urn:sha1:abc"],
)
def test_parse_magnet_rejects_non_magnets_and_hashless_magnets(uri):
    with pytest.raises(ValueError):
        parse_magnet(uri)


# --- SELECTION ---
def test_format_selection_sorts_and_joins_the_indexes():
    assert format_selection([3, 1, 2]) == "1,2,3"


def test_format_selection_rejects_an_empty_selection():
    with pytest.raises(ValueError, match="at least one file"):
        format_selection([])
