"""Trusted-proxy and client-IP resolution.

The trust model: the socket peer is ground truth. Forwarding headers
(``X-Forwarded-For``, CDN-specific headers) are only honored when the
socket peer is itself inside a known trusted set. The XFF chain is
walked right-to-left, skipping hops that are also trusted — the first
untrusted hop is the client.

The trusted set combines three sources:

* explicit CIDRs / IPs from :class:`~skrift.config.TrustedProxyConfig.trusted`
* auto-detected networks (K8s, Docker, loopback, RFC 1918)
* CDN presets and user-configured :class:`TrustedProxySourceConfig` entries

All three collapse into a single :class:`TrustedProxies` snapshot held by
the :class:`TrustedProxyManager`. Snapshots are replaced atomically when
pluggable sources refresh, so resolution is always against a consistent view.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from skrift.config import TrustedProxyConfig, TrustedProxySourceConfig

logger = logging.getLogger(__name__)

# Commonly-referenced network ranges.
PRIVATE_NETWORKS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",  # RFC 4193 unique local addresses
)
LOOPBACK_NETWORKS = ("127.0.0.0/8", "::1/128")


# Built-in CDN presets. Each entry declares:
#   - the CDN-specific client-IP header
#   - one or more source definitions (refreshable range lists)
# Source definitions mirror TrustedProxySourceConfig fields.
CDN_PRESETS: dict[str, dict[str, Any]] = {
    "cloudflare": {
        "header": "cf-connecting-ip",
        "sources": [
            {
                "name": "cloudflare-v4",
                "url": "https://www.cloudflare.com/ips-v4",
                "format": "text",
                "refresh_interval": "24h",
                "fallback": "cloudflare_v4.txt",
            },
            {
                "name": "cloudflare-v6",
                "url": "https://www.cloudflare.com/ips-v6",
                "format": "text",
                "refresh_interval": "24h",
                "fallback": "cloudflare_v6.txt",
            },
        ],
    },
    "fastly": {
        "header": "fastly-client-ip",
        "sources": [
            {
                "name": "fastly",
                "url": "https://api.fastly.com/public-ip-list",
                "format": "json",
                "path": "addresses",
                "refresh_interval": "24h",
                "fallback": "fastly.json",
            },
            {
                "name": "fastly-v6",
                "url": "https://api.fastly.com/public-ip-list",
                "format": "json",
                "path": "ipv6_addresses",
                "refresh_interval": "24h",
                "fallback": "fastly.json",
            },
        ],
    },
    "cloudfront": {
        "header": "cloudfront-viewer-address",
        "sources": [
            {
                "name": "cloudfront",
                "url": "https://ip-ranges.amazonaws.com/ip-ranges.json",
                "format": "json",
                "path": "prefixes[?service=CLOUDFRONT].ip_prefix",
                "refresh_interval": "24h",
                "fallback": "cloudfront.json",
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# TrustedProxies snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedProxies:
    """Immutable snapshot of trusted proxy networks."""

    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    def __contains__(self, ip_str: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip_str.strip())
        except (ValueError, AttributeError):
            return False
        # Normalize IPv4-mapped IPv6 (``::ffff:10.0.0.5``) to IPv4 for matching.
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        for net in self.networks:
            if isinstance(addr, ipaddress.IPv4Address) and isinstance(net, ipaddress.IPv4Network):
                if addr in net:
                    return True
            elif isinstance(addr, ipaddress.IPv6Address) and isinstance(net, ipaddress.IPv6Network):
                if addr in net:
                    return True
        return False

    def combined_with(self, other: "TrustedProxies") -> "TrustedProxies":
        return TrustedProxies(networks=self.networks + other.networks)

    def __repr__(self) -> str:
        count = len(self.networks)
        if count == 0:
            return "TrustedProxies(empty)"
        sample = ", ".join(str(n) for n in self.networks[:3])
        suffix = ", ..." if count > 3 else ""
        return f"TrustedProxies({count} networks: {sample}{suffix})"

    @classmethod
    def from_strings(cls, values: Iterable[str]) -> "TrustedProxies":
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for v in values:
            v = v.strip() if isinstance(v, str) else ""
            if not v:
                continue
            try:
                networks.append(ipaddress.ip_network(v, strict=False))
            except ValueError:
                logger.warning("Skipping invalid trusted_proxy entry: %r", v)
        return cls(networks=tuple(networks))


EMPTY_TRUSTED_PROXIES = TrustedProxies()


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------


def detect_kubernetes() -> bool:
    """Return True when the process appears to be running inside Kubernetes."""
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    try:
        return Path("/var/run/secrets/kubernetes.io/serviceaccount").is_dir()
    except OSError:
        return False


def detect_docker() -> bool:
    """Return True when the process appears to be running inside a container."""
    try:
        if Path("/.dockerenv").exists():
            return True
    except OSError:
        pass
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except OSError:
        return False
    return "docker" in cgroup or "containerd" in cgroup or "kubepods" in cgroup


def auto_detected_cidrs(config: TrustedProxyConfig) -> list[str]:
    """Compute CIDRs to trust based on the runtime environment."""
    if config.explicit:
        return []
    cidrs: list[str] = list(LOOPBACK_NETWORKS)
    is_k8s = detect_kubernetes()
    is_docker = detect_docker()
    trust_private = config.trust_private_networks
    if trust_private is None:
        trust_private = is_k8s or is_docker
    if trust_private:
        cidrs.extend(PRIVATE_NETWORKS)
    return cidrs


# ---------------------------------------------------------------------------
# Client IP resolution
# ---------------------------------------------------------------------------


class StrictResolutionError(Exception):
    """Raised by :func:`resolve_client_ip` in strict mode when resolution fails."""


def _get_header_value(scope, name: str) -> str | None:
    target = name.lower().encode()
    for header in scope.get("headers", []):
        if len(header) != 2:
            continue
        key, value = header
        if isinstance(key, (bytes, bytearray)) and key.lower() == target:
            try:
                return bytes(value).decode("latin-1")
            except (UnicodeDecodeError, AttributeError):
                return None
    return None


def _parse_xff(value: str) -> list[str]:
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def _is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except (ValueError, AttributeError):
        return False
    return True


def resolve_client_ip(
    scope,
    trusted: TrustedProxies,
    *,
    client_ip_header: str = "x-forwarded-for",
    cdn_header: str | None = None,
    max_hops: int = 5,
    strict: bool = False,
) -> tuple[str, str]:
    """Resolve the client IP from an ASGI scope using the trust model.

    Returns ``(ip, provenance)``. Provenance is one of:

    * ``"socket"`` — used the socket peer (untrusted peer, or no header)
    * ``"xff"`` — first untrusted hop walking right-to-left through XFF
    * ``"xff-all-trusted"`` — every hop was trusted (non-strict fallback)
    * the ``cdn_header`` value when the CDN-specific header supplied the IP
    """
    client = scope.get("client")
    peer = client[0] if client else "unknown"

    if peer not in trusted:
        return peer, "socket"

    cdn_value = _get_header_value(scope, cdn_header) if cdn_header else None
    if cdn_value is not None:
        candidate = cdn_value.strip()
        if _is_valid_ip(candidate):
            return candidate, cdn_header  # type: ignore[return-value]
        if strict:
            raise StrictResolutionError(f"malformed {cdn_header}: {candidate!r}")

    xff_value = _get_header_value(scope, client_ip_header)
    if xff_value is None:
        if strict:
            raise StrictResolutionError(
                "trusted peer sent no forwarding header"
            )
        return peer, "socket"

    chain = _parse_xff(xff_value)
    if not chain:
        if strict:
            raise StrictResolutionError("empty forwarding header")
        return peer, "socket"

    if len(chain) > max_hops:
        if strict:
            raise StrictResolutionError(
                f"XFF chain exceeds max_hops ({len(chain)} > {max_hops})"
            )
        logger.warning(
            "XFF chain length %d exceeds max_hops %d; truncating",
            len(chain),
            max_hops,
        )
        chain = chain[-max_hops:]

    for addr in reversed(chain):
        if not _is_valid_ip(addr):
            if strict:
                raise StrictResolutionError(f"malformed address in XFF: {addr!r}")
            continue
        if addr in trusted:
            continue
        return addr, "xff"

    if strict:
        raise StrictResolutionError("every hop in XFF is a trusted proxy")
    return chain[0], "xff-all-trusted"


# ---------------------------------------------------------------------------
# Duration & source parsing
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_DURATION_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_MIN_REFRESH_INTERVAL = 300.0  # 5 minutes


def parse_duration(value: str) -> float:
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"invalid duration: {value!r}")
    n = int(match.group(1))
    unit = match.group(2)
    return max(float(n * _DURATION_UNIT_SECONDS[unit]), _MIN_REFRESH_INTERVAL)


def _extract_simple_path(data: Any, path: str) -> list[str]:
    """Extract CIDR strings from parsed JSON using a minimal path grammar.

    Supported forms:
      * ``"key"`` — value at top-level key (expected list-of-strings)
      * ``"key[*].sub"`` — iterate objects at ``key``, take ``.sub`` from each
      * ``"key[?filter=value].sub"`` — iterate, filter items where
        ``filter == "value"`` (string match), take ``.sub``.
    """
    # Single dotted key, no array access.
    if "[" not in path:
        current: Any = data
        for segment in path.split("."):
            if not segment:
                continue
            current = current[segment]
        if not isinstance(current, list):
            raise ValueError(f"path {path!r} did not resolve to a list")
        return [str(x) for x in current]

    match = re.match(r"^([A-Za-z0-9_]+)\[(\*|\?[^\]]+)\]\.([A-Za-z0-9_.]+)$", path)
    if not match:
        raise ValueError(f"unsupported path: {path!r}")
    key, predicate, sub = match.groups()
    items = data[key]
    if predicate.startswith("?"):
        pred_match = re.match(r"^\?([A-Za-z0-9_]+)\s*=\s*(.+)$", predicate)
        if not pred_match:
            raise ValueError(f"unsupported predicate: {predicate!r}")
        filter_key, filter_value = pred_match.groups()
        items = [item for item in items if str(item.get(filter_key)) == filter_value]
    results: list[str] = []
    for item in items:
        current = item
        for segment in sub.split("."):
            current = current[segment]
        results.append(str(current))
    return results


def parse_source_body(body: str, format: str, path: str | None) -> list[str]:
    """Parse an IP-range source response body into a list of CIDR strings."""
    if format == "text":
        return [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    if format == "cidr-list":
        data = json.loads(body)
        if not isinstance(data, list):
            raise ValueError("cidr-list format expects a JSON array")
        return [str(entry) for entry in data]
    if format == "json":
        if not path:
            raise ValueError("json format requires 'path'")
        return _extract_simple_path(json.loads(body), path)
    raise ValueError(f"unknown source format: {format!r}")


# ---------------------------------------------------------------------------
# Bundled preset data
# ---------------------------------------------------------------------------


def _load_bundled(fallback_name: str) -> str | None:
    """Read a bundled preset file from ``skrift/data/trusted_proxies/``."""
    try:
        data_root = resources.files("skrift.data.trusted_proxies")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    candidate = data_root / fallback_name
    try:
        return candidate.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def _load_fallback(fallback: str | None) -> str | None:
    if not fallback:
        return None
    # Absolute path wins.
    candidate = Path(fallback)
    if candidate.is_absolute() and candidate.exists():
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            return None
    return _load_bundled(fallback)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


@dataclass
class _SourceState:
    name: str
    url: str
    format: str
    path: str | None
    refresh_interval: float
    fallback: str | None
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    task: asyncio.Task | None = None


class TrustedProxyManager:
    """Owns the effective ``TrustedProxies`` set for the running app.

    Combines:
      * auto-detected networks (K8s/Docker/loopback/RFC1918)
      * explicit ``config.trusted`` entries
      * CDN preset sources and user-defined pluggable sources

    Sources are refreshed in the background; each refresh atomically rebuilds
    the combined snapshot. Resolution always sees a consistent view.
    """

    def __init__(self, config: TrustedProxyConfig) -> None:
        self._config = config
        self._static: TrustedProxies = EMPTY_TRUSTED_PROXIES
        self._sources: list[_SourceState] = []
        self._snapshot: TrustedProxies = EMPTY_TRUSTED_PROXIES
        self._cdn_header: str | None = config.cdn_header
        self._rebuild_static()
        self._register_sources()
        self._rebuild_snapshot()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def config(self) -> TrustedProxyConfig:
        return self._config

    @property
    def cdn_header(self) -> str | None:
        return self._cdn_header

    def get(self) -> TrustedProxies:
        return self._snapshot

    def resolve(self, scope) -> tuple[str, str]:
        """Resolve the client IP for an ASGI scope. May raise StrictResolutionError."""
        return resolve_client_ip(
            scope,
            self._snapshot,
            client_ip_header=self._config.client_ip_header,
            cdn_header=self._cdn_header,
            max_hops=self._config.max_hops,
            strict=self._config.strict,
        )

    async def start(self) -> None:
        """Kick off periodic refresh tasks for each registered source."""
        for source in self._sources:
            if source.url:
                source.task = asyncio.create_task(
                    self._refresh_loop(source), name=f"trusted-proxy-{source.name}"
                )

    async def stop(self) -> None:
        """Cancel and await all refresh tasks."""
        tasks = [s.task for s in self._sources if s.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Trusted-proxy source raised during shutdown", exc_info=True)
        for source in self._sources:
            source.task = None

    # ------------------------------------------------------------------
    # Static + source bookkeeping
    # ------------------------------------------------------------------

    def _rebuild_static(self) -> None:
        cidrs: list[str] = []
        cidrs.extend(auto_detected_cidrs(self._config))
        cidrs.extend(self._config.trusted)
        self._static = TrustedProxies.from_strings(cidrs)

    def _register_sources(self) -> None:
        from skrift.config import TrustedProxySourceConfig  # local to avoid cycle

        disabled = {name.lower() for name in self._config.disabled_sources}
        specs: list[tuple[str, dict[str, Any]]] = []

        if self._config.cdn:
            preset = CDN_PRESETS.get(self._config.cdn.lower())
            if preset is None:
                logger.warning("Unknown trusted_proxy.cdn preset %r", self._config.cdn)
            else:
                if self._cdn_header is None:
                    self._cdn_header = preset["header"]
                for source_dict in preset["sources"]:
                    specs.append(("preset", source_dict))

        for source_cfg in self._config.sources:
            # Pydantic model → plain dict via model_dump
            if isinstance(source_cfg, TrustedProxySourceConfig):
                specs.append(("user", source_cfg.model_dump()))
            else:  # dict fallback for programmatic construction
                specs.append(("user", dict(source_cfg)))

        for origin, spec in specs:
            name = str(spec.get("name") or "")
            if not name or name.lower() in disabled:
                continue
            try:
                interval = parse_duration(str(spec.get("refresh_interval") or "24h"))
            except ValueError as exc:
                logger.warning("Invalid refresh_interval for source %s: %s", name, exc)
                interval = parse_duration("24h")
            source = _SourceState(
                name=name,
                url=str(spec.get("url") or ""),
                format=str(spec.get("format") or "text"),
                path=spec.get("path"),
                refresh_interval=interval,
                fallback=spec.get("fallback"),
            )
            # Seed with bundled fallback immediately so snapshots are useful at boot.
            seeded = self._load_from_fallback(source)
            source.networks = seeded
            self._sources.append(source)

    def _load_from_fallback(
        self, source: _SourceState
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        body = _load_fallback(source.fallback)
        if body is None:
            return ()
        try:
            cidrs = parse_source_body(body, source.format, source.path)
        except Exception:
            logger.warning("Failed to parse fallback for source %s", source.name, exc_info=True)
            return ()
        return TrustedProxies.from_strings(cidrs).networks

    def _rebuild_snapshot(self) -> None:
        combined = self._static.networks
        for source in self._sources:
            combined = combined + source.networks
        self._snapshot = TrustedProxies(networks=combined)

    # ------------------------------------------------------------------
    # Refresh loop
    # ------------------------------------------------------------------

    async def _refresh_loop(self, source: _SourceState) -> None:
        # First fetch attempt happens shortly after startup so we don't stall
        # the event loop on boot but still try to freshen bundled data.
        await asyncio.sleep(5.0)
        while True:
            try:
                await self._refresh_source(source)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Trusted-proxy source %s refresh failed", source.name, exc_info=True
                )
            try:
                await asyncio.sleep(source.refresh_interval)
            except asyncio.CancelledError:
                raise

    async def _refresh_source(self, source: _SourceState) -> None:
        if not source.url:
            return
        try:
            import httpx
        except ImportError:
            return  # httpx is a hard dependency, but guard just in case

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(source.url)
            response.raise_for_status()
            body = response.text

        try:
            cidrs = parse_source_body(body, source.format, source.path)
        except Exception:
            logger.warning(
                "Trusted-proxy source %s returned unparseable payload", source.name, exc_info=True
            )
            return

        new_networks = TrustedProxies.from_strings(cidrs).networks
        if not new_networks:
            logger.warning("Trusted-proxy source %s returned no valid CIDRs", source.name)
            return
        source.networks = new_networks
        self._rebuild_snapshot()
        logger.info("Trusted-proxy source %s refreshed (%d ranges)", source.name, len(new_networks))
