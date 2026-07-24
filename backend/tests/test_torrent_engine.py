"""Torrent Downloader: pure engine units (bencode, magnet, filter)."""

from __future__ import annotations

import base64
import hashlib
import os
import time

import pytest
from fake_aria2 import VERSION, FakeAria2

from toolkit_engine import aria2
from toolkit_engine.aria2 import Aria2Error, Aria2RPC, daemon_flags, probe
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


def test_format_selection_builds_aria2_syntax():
    assert format_selection([3, 1, 2]) == "1,2,3"


def test_format_selection_rejects_an_empty_selection():
    # aria2 treats select-file="" as a silent no-op, and a torrent with every
    # file deselected flips straight to "complete" with nothing downloaded.
    with pytest.raises(ValueError, match="at least one file"):
        format_selection([])


# =======================================================
# RPC CLIENT
# =======================================================
@pytest.fixture
def fake_aria2():
    server = FakeAria2()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def rpc(fake_aria2):
    return Aria2RPC(url=fake_aria2.url, secret=fake_aria2.secret)


def test_rpc_reads_the_daemon_version(rpc):
    assert rpc.version() == VERSION


def test_rpc_raises_on_a_bad_secret(fake_aria2):
    bad = Aria2RPC(url=fake_aria2.url, secret="wrong")
    with pytest.raises(Aria2Error, match="Unauthorized"):
        bad.version()


def test_rpc_never_routes_loopback_through_a_proxy():
    # A configured HTTP proxy would intercept 127.0.0.1 and answer with a
    # non-JSON error page; the daemon must always be reached directly.
    assert Aria2RPC()._session.trust_env is False


def test_rpc_treats_a_non_json_response_as_unreachable(monkeypatch):
    # A proxy 503 (or any non-JSON body) must be reported as unreachable, not
    # crash startup with a JSONDecodeError -- the actual make-start failure.
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    rpc = Aria2RPC(secret="x")
    monkeypatch.setattr(rpc._session, "post", lambda *a, **k: FakeResponse())
    with pytest.raises(Aria2Error, match="non-JSON"):
        rpc.version()
    assert probe(rpc) is None


def test_rpc_treats_an_http_error_status_as_unreachable(monkeypatch):
    import requests

    class FakeResponse:
        def raise_for_status(self):
            raise requests.HTTPError("503 Server Error")

        def json(self):  # pragma: no cover - must never be reached
            raise AssertionError("json() must not run on a 5xx")

    rpc = Aria2RPC(secret="x")
    monkeypatch.setattr(rpc._session, "post", lambda *a, **k: FakeResponse())
    assert probe(rpc) is None


def test_rpc_raises_a_clear_error_when_the_daemon_is_unreachable():
    dead = Aria2RPC(url="http://127.0.0.1:1/jsonrpc", secret="x", timeout=0.5)
    with pytest.raises(Aria2Error, match="not reachable"):
        dead.version()


def test_probe_returns_none_instead_of_raising_when_unreachable():
    dead = Aria2RPC(url="http://127.0.0.1:1/jsonrpc", secret="x", timeout=0.5)
    assert probe(dead) is None


def test_probe_returns_the_version_when_reachable(rpc):
    assert probe(rpc) == VERSION


def test_add_uri_passes_options_through(rpc, fake_aria2):
    gid = rpc.add_uri(["magnet:?xt=urn:btih:abc"], {"pause-metadata": "true"})
    assert fake_aria2.downloads[gid]["options"] == {"pause-metadata": "true"}


def test_tell_all_flattens_the_multicall(rpc, fake_aria2):
    fake_aria2.add_download(infohash="aa", status="active")
    fake_aria2.add_download(infohash="bb", status="paused")
    fake_aria2.add_download(infohash="cc", status="complete")

    hashes = {d["infoHash"] for d in rpc.tell_all()}
    assert hashes == {"aa", "bb", "cc"}


# =======================================================
# DAEMON SUPERVISION
# =======================================================
def flags(tmp_path):
    return daemon_flags(
        state_dir=tmp_path / "state",
        download_dir=tmp_path / "dl",
        secret="tok",
        port=6800,
    )


def test_daemon_flags_enable_rpc_and_pause_metadata(tmp_path):
    # pause-metadata is silently ignored unless RPC is on -- and without it,
    # content downloads start immediately and the whole filter is defeated.
    assert "--enable-rpc=true" in flags(tmp_path)
    assert "--pause-metadata=true" in flags(tmp_path)


def test_daemon_flags_persist_the_session_on_a_timer(tmp_path):
    session = tmp_path / "state" / aria2.SESSION_FILENAME
    got = flags(tmp_path)
    assert f"--save-session={session}" in got
    # aria2 does NOT auto-load its own session file; --input-file is required.
    assert f"--input-file={session}" in got
    # Defaults to 0, meaning "write only on clean exit" -- a kill -9 would
    # otherwise drop the entire queue and every file selection with it.
    assert "--save-session-interval=30" in got


def test_daemon_flags_reuse_saved_metadata(tmp_path):
    # Both default to false; without them every restart re-fetches metadata
    # from the DHT, which on a dead swarm looks like a hung resume.
    assert "--bt-save-metadata=true" in flags(tmp_path)
    assert "--bt-load-saved-metadata=true" in flags(tmp_path)


def test_daemon_flags_bind_loopback_only(tmp_path):
    assert "--rpc-listen-all=false" in flags(tmp_path)
    assert "--rpc-secret=tok" in flags(tmp_path)


def test_daemon_flags_do_not_seed_after_completion(tmp_path):
    assert "--seed-time=0" in flags(tmp_path)


def test_spawn_reports_a_clear_error_when_aria2_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(aria2.shutil, "which", lambda _name: None)
    with pytest.raises(Aria2Error, match="brew install aria2"):
        aria2.spawn(
            state_dir=tmp_path / "state",
            download_dir=tmp_path / "dl",
            secret="tok",
        )


def test_spawn_creates_the_session_file_before_launching(tmp_path, monkeypatch):
    launched = {}

    def fake_popen(cmd, **kwargs):
        launched["cmd"] = cmd
        return object()

    monkeypatch.setattr(aria2.shutil, "which", lambda _name: "/usr/local/bin/aria2c")
    monkeypatch.setattr(aria2.subprocess, "Popen", fake_popen)

    state = tmp_path / "state"
    aria2.spawn(state_dir=state, download_dir=tmp_path / "dl", secret="tok")

    # aria2 errors at startup if --input-file points at a missing path.
    assert (state / aria2.SESSION_FILENAME).exists()
    assert launched["cmd"][0] == "/usr/local/bin/aria2c"


# =======================================================
# DAEMON LIFECYCLE
# =======================================================
def test_secret_is_generated_once_then_reused(tmp_path):
    path = tmp_path / "aria2-secret"
    first = aria2.read_or_create_secret(path)

    assert first and path.exists()
    # A restart must read the SAME secret, or it cannot authenticate against a
    # daemon a previous run left behind -- the whole point of persisting it.
    assert aria2.read_or_create_secret(path) == first


def test_secret_file_is_not_world_readable(tmp_path):
    path = tmp_path / "aria2-secret"
    aria2.read_or_create_secret(path)
    assert (path.stat().st_mode & 0o077) == 0


def test_pid_file_round_trips(tmp_path):
    path = tmp_path / "aria2.pid"
    aria2.write_pid(path, 4242)
    assert aria2.read_pid(path) == 4242


def test_read_pid_is_none_when_absent_or_garbage(tmp_path):
    assert aria2.read_pid(tmp_path / "missing.pid") is None
    junk = tmp_path / "junk.pid"
    junk.write_text("not-a-pid")
    assert aria2.read_pid(junk) is None


def test_pid_file_names_a_live_process_tracks_ownership(tmp_path):
    path = tmp_path / "aria2.pid"
    # Our own process is certainly alive -> "ours".
    aria2.write_pid(path, os.getpid())
    assert aria2.pid_file_names_a_live_process(path) is True


def test_pid_file_names_a_live_process_is_false_for_a_dead_pid(tmp_path):
    path = tmp_path / "aria2.pid"
    # PID 2**31-1 is astronomically unlikely to exist.
    aria2.write_pid(path, 2**31 - 1)
    assert aria2.pid_file_names_a_live_process(path) is False


def test_pid_file_names_a_live_process_is_false_when_no_file(tmp_path):
    # No PID file == external daemon (brew services); never adopt it as ours.
    assert aria2.pid_file_names_a_live_process(tmp_path / "none.pid") is False


def test_port_is_open_is_false_on_a_free_port():
    # Port 1 is privileged and never bound by this app.
    assert aria2.port_is_open(port=1) is False


def test_stop_process_removes_the_pid_file(tmp_path, monkeypatch):
    path = tmp_path / "aria2.pid"
    aria2.write_pid(path, 999999)
    # Pretend the process is already gone, so no real signal is sent.
    monkeypatch.setattr(aria2, "pid_is_alive", lambda _pid: False)

    aria2.stop_process(path)
    assert not path.exists()


def test_stop_process_signals_a_live_daemon_then_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "aria2.pid"
    aria2.write_pid(path, 555)
    signalled = []
    monkeypatch.setattr(aria2, "pid_is_alive", lambda pid: pid == 555 and not signalled)
    monkeypatch.setattr(aria2.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    aria2.stop_process(path, timeout=0.5)

    assert signalled and signalled[0] == (555, aria2.signal.SIGTERM)
    assert not path.exists()


def test_wait_until_ready_gives_up_within_the_bound(tmp_path):
    dead = Aria2RPC(url="http://127.0.0.1:1/jsonrpc", secret="x", timeout=0.2)
    started = time.monotonic()
    assert aria2.wait_until_ready(dead, timeout=0.5) is False
    # Bounded: it must not run anywhere near the per-call request timeout * N.
    assert time.monotonic() - started < 2.0
