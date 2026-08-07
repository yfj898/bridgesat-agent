"""Acceptance test 5: crawler blocks localhost and private-network
redirects (SSRF).

THREAT_MODEL.md section 5.7: only approved registered sources may be
fetched; private, loopback, link-local, multicast, reserved, and
unspecified addresses are blocked; every redirect hop is revalidated with
the same rules before it is followed.
"""

from __future__ import annotations

import pytest

from app.ingestion.fetcher import FetchError, SafeFetcher

BLOCKED_URLS = [
    ("https://localhost/data", "localhost"),
    ("https://127.0.0.1/data", "127.0.0.1"),
    ("https://[::1]/data", "::1"),
    ("https://10.0.0.1/data", "10.0.0.1"),
    ("https://172.16.0.1/data", "172.16.0.1"),
    ("https://192.168.1.1/data", "192.168.1.1"),
    ("https://169.254.169.254/latest/meta-data/", "169.254.169.254"),
    ("https://0.0.0.0/data", "0.0.0.0"),
]


@pytest.mark.parametrize(("url", "host"), BLOCKED_URLS)
def test_private_and_loopback_urls_rejected(url: str, host: str) -> None:
    """Even when a source registry (wrongly) allowlists a private address,
    the address validation must block it before any connection is made."""
    fetcher = SafeFetcher()
    with pytest.raises(FetchError, match="blocked non-public destination"):
        fetcher._validate_url(url, allowed_hosts={host})


def test_unapproved_public_host_rejected() -> None:
    fetcher = SafeFetcher()
    with pytest.raises(FetchError, match="not in the source allowlist"):
        fetcher._validate_url("https://example.com/data", allowed_hosts={"trusted.example"})


def test_non_https_scheme_rejected() -> None:
    fetcher = SafeFetcher()
    with pytest.raises(FetchError, match="HTTPS"):
        fetcher._validate_url("http://trusted.example/data", allowed_hosts={"trusted.example"})


def test_redirect_to_private_host_never_followed() -> None:
    """The redirect hop validator applies the same private-address rules as
    the initial URL: a Location header pointing at loopback must fail
    validation before any connection to the hop is attempted."""
    fetcher = SafeFetcher()
    with pytest.raises(FetchError, match="blocked non-public destination"):
        fetcher._validate_url(
            "https://127.0.0.1/redirect-target", allowed_hosts={"127.0.0.1"}
        )


def test_redirect_to_public_allowlisted_host_passes_validation() -> None:
    fetcher = SafeFetcher()
    parsed = fetcher._validate_url(
        "https://storage.googleapis.com/redirect-target",
        allowed_hosts={"public.example", "storage.googleapis.com"},
    )
    assert parsed.hostname == "storage.googleapis.com"


def test_public_url_validation_passes() -> None:
    fetcher = SafeFetcher()
    parsed = fetcher._validate_url(
        "https://storage.googleapis.com/data.zip",
        allowed_hosts={"storage.googleapis.com"},
    )
    assert parsed.hostname == "storage.googleapis.com"
