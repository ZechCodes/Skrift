"""Tests for trusted-proxy client IP resolution."""

import ipaddress

import pytest

from skrift.config import TrustedProxyConfig
from skrift.lib.trusted_proxy import (
    CDN_PRESETS,
    EMPTY_TRUSTED_PROXIES,
    StrictResolutionError,
    TrustedProxies,
    TrustedProxyManager,
    _extract_simple_path,
    auto_detected_cidrs,
    parse_duration,
    parse_source_body,
    resolve_client_ip,
)


def _scope(peer: str = "203.0.113.7", headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": headers or [],
        "client": (peer, 0),
    }


class TestTrustedProxiesMembership:
    def test_empty_contains_nothing(self):
        assert "10.0.0.1" not in EMPTY_TRUSTED_PROXIES

    def test_ipv4_cidr_match(self):
        tp = TrustedProxies.from_strings(["10.0.0.0/8"])
        assert "10.5.6.7" in tp
        assert "192.168.1.1" not in tp

    def test_invalid_entries_skipped(self):
        tp = TrustedProxies.from_strings(["10.0.0.0/8", "not-an-ip", ""])
        assert len(tp.networks) == 1

    def test_ipv4_mapped_ipv6_matches_ipv4_cidr(self):
        tp = TrustedProxies.from_strings(["10.0.0.0/8"])
        assert "::ffff:10.5.6.7" in tp

    def test_ipv6_cidr(self):
        tp = TrustedProxies.from_strings(["2400:cb00::/32"])
        assert "2400:cb00:0:1::1" in tp
        assert "2606:4700::1" not in tp

    def test_invalid_input_returns_false(self):
        tp = TrustedProxies.from_strings(["10.0.0.0/8"])
        assert "garbage" not in tp
        assert "" not in tp

    def test_repr_truncates(self):
        tp = TrustedProxies.from_strings(
            ["10.0.0.0/8", "11.0.0.0/8", "12.0.0.0/8", "13.0.0.0/8"]
        )
        assert "4 networks" in repr(tp)
        assert "..." in repr(tp)

    def test_combined_with(self):
        a = TrustedProxies.from_strings(["10.0.0.0/8"])
        b = TrustedProxies.from_strings(["192.168.0.0/16"])
        c = a.combined_with(b)
        assert "10.0.0.1" in c
        assert "192.168.1.1" in c


class TestResolveClientIP:
    def test_untrusted_peer_ignores_xff(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="203.0.113.7",
            headers=[(b"x-forwarded-for", b"1.2.3.4")],
        )
        ip, source = resolve_client_ip(scope, trusted)
        assert ip == "203.0.113.7"
        assert source == "socket"

    def test_trusted_peer_honors_xff(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"203.0.113.7")],
        )
        ip, source = resolve_client_ip(scope, trusted)
        assert ip == "203.0.113.7"
        assert source == "xff"

    def test_walks_right_to_left_skipping_trusted(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8", "198.51.100.0/24"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"203.0.113.7, 198.51.100.2, 10.0.0.5")],
        )
        ip, source = resolve_client_ip(scope, trusted)
        assert ip == "203.0.113.7"
        assert source == "xff"

    def test_all_hops_trusted_returns_leftmost(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8", "198.51.100.0/24"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"198.51.100.2, 10.0.0.5")],
        )
        ip, source = resolve_client_ip(scope, trusted)
        assert ip == "198.51.100.2"
        assert source == "xff-all-trusted"

    def test_strict_all_trusted_raises(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"10.0.0.1, 10.0.0.2")],
        )
        with pytest.raises(StrictResolutionError):
            resolve_client_ip(scope, trusted, strict=True)

    def test_strict_no_header_on_trusted_peer_raises(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(peer="10.0.0.5", headers=[])
        with pytest.raises(StrictResolutionError):
            resolve_client_ip(scope, trusted, strict=True)

    def test_nonstrict_no_header_falls_back_to_socket(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(peer="10.0.0.5", headers=[])
        ip, source = resolve_client_ip(scope, trusted)
        assert ip == "10.0.0.5"
        assert source == "socket"

    def test_malformed_hop_skipped_when_nonstrict(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"203.0.113.7, garbage")],
        )
        ip, source = resolve_client_ip(scope, trusted)
        assert ip == "203.0.113.7"
        assert source == "xff"

    def test_malformed_hop_raises_in_strict(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"garbage")],
        )
        with pytest.raises(StrictResolutionError):
            resolve_client_ip(scope, trusted, strict=True)

    def test_max_hops_truncates(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        # Build an XFF chain longer than max_hops with all trusted hops
        chain = b", ".join([b"10.0.0." + str(i).encode() for i in range(1, 12)])
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", chain)],
        )
        ip, source = resolve_client_ip(scope, trusted, max_hops=3)
        # Truncated to last 3 entries, all trusted → xff-all-trusted
        assert source == "xff-all-trusted"
        # Leftmost of the truncated chain
        assert ip == "10.0.0.9"

    def test_max_hops_strict_raises(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2, 3.3.3.3, 4.4.4.4, 5.5.5.5, 6.6.6.6")],
        )
        with pytest.raises(StrictResolutionError):
            resolve_client_ip(scope, trusted, max_hops=3, strict=True)

    def test_cdn_header_takes_precedence_over_xff(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[
                (b"x-forwarded-for", b"203.0.113.7"),
                (b"cf-connecting-ip", b"198.51.100.9"),
            ],
        )
        ip, source = resolve_client_ip(scope, trusted, cdn_header="cf-connecting-ip")
        assert ip == "198.51.100.9"
        assert source == "cf-connecting-ip"

    def test_cdn_header_ignored_when_peer_untrusted(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="203.0.113.7",
            headers=[(b"cf-connecting-ip", b"198.51.100.9")],
        )
        ip, source = resolve_client_ip(scope, trusted, cdn_header="cf-connecting-ip")
        assert ip == "203.0.113.7"
        assert source == "socket"

    def test_malformed_cdn_header_falls_back_to_xff(self):
        trusted = TrustedProxies.from_strings(["10.0.0.0/8"])
        scope = _scope(
            peer="10.0.0.5",
            headers=[
                (b"cf-connecting-ip", b"not-an-ip"),
                (b"x-forwarded-for", b"203.0.113.7"),
            ],
        )
        ip, source = resolve_client_ip(scope, trusted, cdn_header="cf-connecting-ip")
        assert ip == "203.0.113.7"
        assert source == "xff"


class TestEnvironmentDetection:
    def test_no_auto_detection_when_explicit(self):
        config = TrustedProxyConfig(explicit=True, trust_private_networks=True)
        assert auto_detected_cidrs(config) == []

    def test_private_networks_off_by_default_bare_metal(self, monkeypatch):
        monkeypatch.setattr("skrift.lib.trusted_proxy.detect_kubernetes", lambda: False)
        monkeypatch.setattr("skrift.lib.trusted_proxy.detect_docker", lambda: False)
        config = TrustedProxyConfig()
        cidrs = auto_detected_cidrs(config)
        assert "127.0.0.0/8" in cidrs
        # No private ranges on bare metal by default
        assert "10.0.0.0/8" not in cidrs

    def test_containerized_defaults_to_trusting_private(self, monkeypatch):
        monkeypatch.setattr("skrift.lib.trusted_proxy.detect_kubernetes", lambda: False)
        monkeypatch.setattr("skrift.lib.trusted_proxy.detect_docker", lambda: True)
        config = TrustedProxyConfig()
        cidrs = auto_detected_cidrs(config)
        assert "10.0.0.0/8" in cidrs

    def test_explicit_trust_private_networks_true(self):
        config = TrustedProxyConfig(trust_private_networks=True)
        cidrs = auto_detected_cidrs(config)
        assert "10.0.0.0/8" in cidrs
        assert "127.0.0.0/8" in cidrs


class TestParseSourceBody:
    def test_text_strips_comments_and_blanks(self):
        body = "# comment\n10.0.0.0/8\n\n11.0.0.0/8\n"
        assert parse_source_body(body, "text", None) == ["10.0.0.0/8", "11.0.0.0/8"]

    def test_cidr_list(self):
        body = '["10.0.0.0/8", "11.0.0.0/8"]'
        assert parse_source_body(body, "cidr-list", None) == ["10.0.0.0/8", "11.0.0.0/8"]

    def test_json_simple_key(self):
        body = '{"addresses": ["10.0.0.0/8"]}'
        assert parse_source_body(body, "json", "addresses") == ["10.0.0.0/8"]

    def test_json_wildcard_list(self):
        body = '{"items": [{"ip": "1.1.1.1/32"}, {"ip": "2.2.2.2/32"}]}'
        assert parse_source_body(body, "json", "items[*].ip") == ["1.1.1.1/32", "2.2.2.2/32"]

    def test_json_filtered_list(self):
        body = (
            '{"prefixes": ['
            '{"ip_prefix": "1.1.1.1/32", "service": "CLOUDFRONT"},'
            '{"ip_prefix": "2.2.2.2/32", "service": "EC2"}'
            ']}'
        )
        result = parse_source_body(body, "json", "prefixes[?service=CLOUDFRONT].ip_prefix")
        assert result == ["1.1.1.1/32"]

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError):
            parse_source_body("x", "unknown", None)

    def test_parse_duration_clamps_to_minimum(self):
        # 1 minute < 5 minute minimum → 300s
        assert parse_duration("1m") == 300.0

    def test_parse_duration_units(self):
        assert parse_duration("1h") == 3600.0
        assert parse_duration("2d") == 2 * 86400.0


class TestTrustedProxyManager:
    def test_empty_config_produces_empty_snapshot(self):
        mgr = TrustedProxyManager(TrustedProxyConfig(explicit=True))
        assert mgr.get().networks == ()

    def test_explicit_trusted_list(self):
        mgr = TrustedProxyManager(
            TrustedProxyConfig(explicit=True, trusted=["10.0.0.0/8"])
        )
        assert "10.0.0.1" in mgr.get()

    def test_cloudflare_preset_loads_bundled_ranges(self):
        mgr = TrustedProxyManager(TrustedProxyConfig(explicit=True, cdn="cloudflare"))
        # Cloudflare 104.16.0.0/13 is in the bundled fallback
        assert "104.16.5.5" in mgr.get()
        assert mgr.cdn_header == "cf-connecting-ip"

    def test_disabled_source_excluded(self):
        mgr = TrustedProxyManager(
            TrustedProxyConfig(
                explicit=True, cdn="cloudflare", disabled_sources=["cloudflare-v4"]
            )
        )
        assert "104.16.5.5" not in mgr.get()  # IPv4 source disabled
        # IPv6 source still loaded
        assert "2400:cb00::1" in mgr.get()

    def test_resolve_uses_snapshot(self):
        mgr = TrustedProxyManager(
            TrustedProxyConfig(explicit=True, trusted=["10.0.0.0/8"])
        )
        scope = _scope(
            peer="10.0.0.5",
            headers=[(b"x-forwarded-for", b"203.0.113.7")],
        )
        ip, source = mgr.resolve(scope)
        assert ip == "203.0.113.7"
        assert source == "xff"

    def test_unknown_cdn_preset_logs_but_proceeds(self):
        mgr = TrustedProxyManager(TrustedProxyConfig(explicit=True, cdn="unknown-cdn"))
        assert mgr.get().networks == ()

    @pytest.mark.asyncio
    async def test_start_and_stop_manager_idempotent(self):
        mgr = TrustedProxyManager(TrustedProxyConfig(explicit=True))
        await mgr.start()
        await mgr.stop()
        # Second stop should be a no-op
        await mgr.stop()


class TestPresetsShape:
    def test_all_presets_have_required_keys(self):
        for name, preset in CDN_PRESETS.items():
            assert "header" in preset, f"{name} missing header"
            assert "sources" in preset, f"{name} missing sources"
            for source in preset["sources"]:
                assert "name" in source
                assert "format" in source
                assert "fallback" in source


class TestExtractSimplePath:
    def test_dotted_key(self):
        data = {"a": {"b": ["x", "y"]}}
        # The current implementation only walks dotted keys if no brackets appear.
        # Nested dotted path support: "a.b"
        assert _extract_simple_path(data, "a.b") == ["x", "y"]

    def test_bracket_star(self):
        data = {"items": [{"x": "a"}, {"x": "b"}]}
        assert _extract_simple_path(data, "items[*].x") == ["a", "b"]
