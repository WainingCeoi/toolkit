"""LAN host launcher: LAN-address classification, port probing, auth token."""

from __future__ import annotations

import os
import socket

import pytest

from toolkit_api import host


@pytest.fixture(autouse=True)
def _restore_auth_token():
    # _ensure_auth_token exports APP_AUTH_TOKEN to the real environment (by
    # design — the host process reads it). Restore it after each test so the
    # token can't leak into later API tests as a spurious 401.
    before = os.environ.get("APP_AUTH_TOKEN")
    yield
    if before is None:
        os.environ.pop("APP_AUTH_TOKEN", None)
    else:
        os.environ["APP_AUTH_TOKEN"] = before


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


def test_ensure_auth_token_local_only_is_none(monkeypatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    assert host._ensure_auth_token(local_only=True) is None
    assert "APP_AUTH_TOKEN" not in host.os.environ


def test_ensure_auth_token_mints_and_exports_for_lan(monkeypatch):
    monkeypatch.delenv("APP_AUTH_TOKEN", raising=False)
    token = host._ensure_auth_token(local_only=False)
    assert token and len(token) >= 8
    assert host.os.environ["APP_AUTH_TOKEN"] == token


def test_ensure_auth_token_honors_a_pinned_value(monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "pinned-secret")
    assert host._ensure_auth_token(local_only=False) == "pinned-secret"


def test_banner_shows_token_when_hosting(capsys):
    host._print_banner("0.0.0.0", 8000, 8000, "mac.local", "192.168.1.5", "tok-123")
    out = capsys.readouterr().out
    assert "tok-123" in out
    assert "192.168.1.5" in out


def test_banner_hides_token_when_local_only(capsys):
    host._print_banner("127.0.0.1", 8000, 8000, None, None, None)
    out = capsys.readouterr().out
    assert "local-only" in out
    assert "Access token" not in out
