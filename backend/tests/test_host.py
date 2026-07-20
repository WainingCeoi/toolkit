"""LAN host launcher: LAN-address classification, port probing, banner."""

from __future__ import annotations

import socket

import pytest

from toolkit_api import host


@pytest.mark.parametrize(
    "ip,expected",
    [
        ("10.0.0.5", True),
        ("192.168.1.20", True),
        ("172.16.4.9", True),
        ("127.0.0.1", False),  # loopback
        ("169.254.10.1", False),  # link-local
        ("8.8.8.8", False),  # public
        ("::1", False),  # not IPv4
        ("not-an-ip", False),
    ],
)
def test_is_private_lan(ip, expected):
    assert host._is_private_lan(ip) is expected


def test_free_port_advances_past_a_bound_port():
    # Bind a port, then ask free_port to start there — it must skip to the next.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        busy = taken.getsockname()[1]
        chosen = host.free_port("127.0.0.1", busy, tries=5)
        assert chosen != busy
        assert busy < chosen <= busy + 4


def test_free_port_ipv6_host_does_not_crash():
    # Regression: an AF_INET-only probe aborted for ::1. getaddrinfo picks the
    # right family now, so a loopback IPv6 bind succeeds.
    chosen = host.free_port("::1", 0, tries=1)
    assert isinstance(chosen, int)


def test_banner_warns_that_a_lan_bind_is_unauthenticated(capsys):
    host._print_banner("0.0.0.0", 8000, 8000, "mac.local", "192.168.1.5")
    out = capsys.readouterr().out
    assert "NO authentication" in out
    assert "EXPOSED ON THE LAN" in out
    assert "192.168.1.5" in out
    assert "mac.local" in out


def test_banner_omits_the_lan_warning_when_local_only(capsys):
    host._print_banner("127.0.0.1", 8000, 8000, None, None)
    out = capsys.readouterr().out
    assert "local-only" in out
    assert "EXPOSED ON THE LAN" not in out


def test_banner_flags_a_port_bump(capsys):
    host._print_banner("127.0.0.1", 8001, 8000, None, None)
    out = capsys.readouterr().out
    assert "8000 was busy" in out
