# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
# network_manager.py

"""Centralized network management for all HTTP activity.

Replaces per-plugin rate limiting, session management, and header
configuration with a single hub. Plugins access the network exclusively
through ``PluginHttpClient`` (SDK), which delegates here.

Key responsibilities:
- Online/offline toggle (integrates with ``NetworkMonitor``)
- Per-plugin domain allowlists (from ``plugin.json``)
- Centralized per-domain rate limiting (cross-plugin)
- Browser-like default headers on all outgoing requests
- Per-plugin connection pooling via ``requests.Session``
- Request statistics per plugin

Image cache gets a dedicated session via ``get_image_session()`` that
is NOT subject to plugin domain allowlists (core functionality).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


# ── Browser-like default headers ─────────────────────────────────────

_DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}

# Known analytics/tracking/telemetry domains — always blocked.
# If a plugin legitimately needs any of these, it must declare the
# domain in its plugin.json allowed_domains (subject to review).
_ANALYTICS_BLOCKLIST = frozenset({
    # ── Web analytics ──

    # Google
    "google-analytics.com",
    "www.google-analytics.com",
    "ssl.google-analytics.com",
    "googletagmanager.com",
    "www.googletagmanager.com",
    "pagead2.googlesyndication.com",
    # Cloudflare
    "cloudflare-analytics.com",
    "static.cloudflareinsights.com",

    # ── Error / crash reporting ──

    # Sentry (o*.ingest.sentry.io is per-org — suffix match covers all)
    "sentry.io",
    "browser.sentry-cdn.com",
    # GlitchTip (open-source Sentry alternative)
    "glitchtip.com",
    # Bugsnag
    "bugsnag.com",
    "notify.bugsnag.com",
    # Rollbar
    "api.rollbar.com",
    # Raygun
    "api.raygun.com",
    "api.raygun.io",
    # Airbrake
    "collect.airbrake.io",
    # Honeybadger
    "api.honeybadger.io",
    # Instabug
    "api.instabug.com",
    # Embrace (mobile crash/perf)
    "api.embrace.io",
    "data.emb-api.com",
    # Backtrace (game crash reporting — Unreal/Unity studios)
    "api.backtrace.io",
    "submit.backtrace.io",
    # Exceptionless (open-source)
    "exceptionless.com",
    "collector.exceptionless.io",
    # TrackJS
    "api.trackjs.com",

    # ── Product analytics ──

    "mixpanel.com",
    "api.mixpanel.com",
    "api-eu.mixpanel.com",
    "decide.mixpanel.com",
    "cdn.mxpnl.com",
    "amplitude.com",
    "api.amplitude.com",
    "api2.amplitude.com",
    "cdn.amplitude.com",
    "heapanalytics.com",
    "cdn.heapanalytics.com",
    "posthog.com",
    "app.posthog.com",
    "us.posthog.com",
    "eu.posthog.com",

    # ── CDP / data pipelines ──

    "api.segment.io",
    "cdn.segment.com",
    "api.segment.com",
    "rudderlabs.com",
    "api.rudderstack.com",
    "cdn.rudderlabs.com",
    "api.mparticle.com",

    # ── Session replay / heatmaps ──

    "hotjar.com",
    "static.hotjar.com",
    "script.hotjar.com",
    "fullstory.com",
    "rs.fullstory.com",
    "edge.fullstory.com",
    "logrocket.com",
    "cdn.logrocket.com",
    "r.lr-ingest.io",
    "lr-in-prod.com",
    "mouseflow.com",
    "cdn.mouseflow.com",
    "smartlook.com",
    "rec.smartlook.com",
    "web-sdk.smartlook.com",
    "luckyorange.com",
    "cdn.luckyorange.com",
    "crazyegg.com",
    "script.crazyegg.com",
    "cdn.inspectlet.com",
    "hn.inspectlet.com",
    # Microsoft Clarity (session replay)
    "clarity.ms",
    "c.clarity.ms",

    # ── UX / A-B testing / feature flags ──

    "app.pendo.io",
    "cdn.pendo.io",
    "pendo-static-6047664892149760.storage.googleapis.com",
    "optimizely.com",
    "cdn.optimizely.com",
    "logx.optimizely.com",
    "vwo.com",
    "dev.visualwebsiteoptimizer.com",
    "abtasty.com",
    "try.abtasty.com",
    # Statsig
    "featureflagservice.statsig.com",
    "api.statsig.com",
    # GrowthBook
    "cdn.growthbook.io",
    # ConfigCat
    "api.configcat.com",
    # Flagsmith
    "api.flagsmith.com",

    # ── APM / infrastructure monitoring ──

    "newrelic.com",
    "js-agent.newrelic.com",
    "bam.nr-data.net",
    "api.datadoghq.com",
    "browser-intake-datadoghq.com",
    "rum.browser-intake-datadoghq.com",
    "app.datadoghq.com",
    "logs.browser-intake-datadoghq.com",
    "trace.browser-intake-datadoghq.com",
    "api.datadoghq.eu",
    "browser-intake-datadoghq.eu",
    "appdynamics.com",
    "cdn.appdynamics.com",
    "api.dynatrace.com",
    "js-cdn.dynatrace.com",
    # Grafana Cloud
    "grafana-prod-01-prod-us-east-0.grafana.net",
    # Honeycomb
    "api.honeycomb.io",
    # Scout APM
    "checkin.scoutapm.com",

    # ── Marketing / email / CRM tracking ──

    "hubspot.com",
    "js.hs-scripts.com",
    "forms.hsforms.com",
    "track.hubspot.com",
    "braze.com",
    "sdk.iad-01.braze.com",
    "marketo.com",
    "munchkin.marketo.net",
    # Drift
    "drift.com",
    "js.driftt.com",
    # OneSignal
    "api.onesignal.com",
    "onesignal.com",
    # Customer.io
    "sdk.customer.io",
    "track.customer.io",
    # CleverTap
    "in.clevertap.com",
    "wzrkt.com",
    # Leanplum
    "api.leanplum.com",
    # MoEngage
    "sdk.moengage.com",

    # ── Intercom / customer tracking ──

    "intercom.io",
    "api.intercom.io",
    "widget.intercom.io",
    "api-iam.intercom.io",

    # ── Privacy-focused analytics (still telemetry) ──

    "plausible.io",
    "usefathom.com",
    "cdn.usefathom.com",
    "simpleanalytics.com",
    "queue.simpleanalytics.cloud",
    "api.pirsch.io",
    "api.swetrix.com",

    # ── Open-source / self-hostable analytics (SaaS endpoints) ──

    "countly.com",
    "api.count.ly",
    "matomo.cloud",
    "api.keen.io",

    # ── Tag management ──

    "tealium.com",
    "tags.tiqcdn.com",
    "collect.tealiumiq.com",

    # ── Social media tracking pixels ──

    # Meta / Facebook
    "connect.facebook.net",
    "pixel.facebook.com",
    "www.facebook.com/tr",
    # TikTok
    "analytics.tiktok.com",
    # Pinterest
    "ct.pinterest.com",
    # LinkedIn
    "snap.licdn.com",
    "px.ads.linkedin.com",
    # Twitter / X
    "analytics.twitter.com",
    "t.co",
    # Bing / Microsoft Ads
    "bat.bing.com",
    # Reddit
    "events.redditmedia.com",
    # Snapchat
    "tr.snapchat.com",
    "sc-static.net",

    # ── Attribution / mobile SDKs ──

    # Adjust
    "app.adjust.com",
    "s2s.adjust.com",
    # AppsFlyer
    "appsflyer.com",
    "t.appsflyer.com",
    "launches.appsflyer.com",
    # Kochava
    "kochava.com",
    "control.kochava.com",
    # Branch
    "api2.branch.io",
    "cdn.branch.io",
    # Tenjin
    "tenjin.com",
    "track.tenjin.com",
    # Flurry (Yahoo)
    "data.flurry.com",
    "gw.flurry.com",

    # ── Fingerprinting / bot detection ──

    "fpjs.io",
    "api.fpjs.io",
    "px-cdn.net",
    "px-client.net",

    # ── SDK / engine telemetry endpoints ──

    # Unity
    "config.uca.cloud.unity3d.com",
    "cdp.cloud.unity3d.com",
    "data-optout-service.uca.cloud.unity3d.com",
    "perf-events.cloud.unity3d.com",
    "remote-config-proxy-prd.uca.cloud.unity3d.com",
    "userreporting.cloud.unity3d.com",
    "analytics.cloud.unity3d.com",
    "stats.unity3d.com",
    "api.uca.cloud.unity3d.com",
    "ecommerce.iap.unity3d.com",
    "unityads.unity3d.com",
    "auction.unityads.unity3d.com",
    "adserver.unityads.unity3d.com",

    # Epic Games / Unreal
    "et-public.epicgames.com",
    "et2-public.epicgames.com",
    "tracking.epicgames.com",
    "datarouter.ol.epicgames.com",
    "metrics.ol.epicgames.com",
    "crash-reporting.ol.epicgames.com",

    # ── Game launcher telemetry ──

    # Valve / Steam
    "crash.steampowered.com",
    # GOG Galaxy
    "gog-galileo.gog.com",
    "insights-collector.gog.com",
    "remote-config.gog.com",
    # EA / Origin
    "telemetry.ea.com",
    "river.data.ea.com",
    "pin-river.data.ea.com",
    # Ubisoft Connect
    "telemetry.ubi.com",
    "ubisoft-orbis-msr.ubi.com",

    # ── Game analytics SDKs ──

    # GameAnalytics
    "api.gameanalytics.com",
    "rubick.gameanalytics.com",
    "sandbox-api.gameanalytics.com",
    # PlayFab (Microsoft)
    "events.playfab.com",
    # deltaDNA (Unity)
    "deltadna.net",
    "collect.deltadna.net",

    # ── Microsoft / VS Code ──

    "dc.services.visualstudio.com",
    "vortex.data.microsoft.com",
    "mobile.events.data.microsoft.com",
    "self.events.data.microsoft.com",
    "telecommand.telemetry.microsoft.com",
    "watson.telemetry.microsoft.com",
    "watson.microsoft.com",
    "settings-win.data.microsoft.com",
    "v10.events.data.microsoft.com",
    "v10c.events.data.microsoft.com",
    "v20.events.data.microsoft.com",
    "browser.events.data.msn.com",
    "az764295.vo.msecnd.net",

    # ── Datadog SDK / agent ──

    "instrumentation-telemetry-intake.datadoghq.com",
    "http-intake.logs.datadoghq.com",
    "process.datadoghq.com",
    "ndm-intake.datadoghq.com",
    "snmp-traps-intake.datadoghq.com",
    "intake.profile.datadoghq.com",
    "instrumentation-telemetry-intake.datadoghq.eu",
    "http-intake.logs.datadoghq.eu",

    # ── Electron / Chromium crash + telemetry ──

    "crashpad.chromium.org",
    "crash-reports.browser.yandex.net",
    "cr-buildbucket.appspot.com",

    # ── JetBrains ──

    "resources.jetbrains.com",
    "download.jetbrains.com/jstatc",
    "stats.jetbrains.com",
    "accounts.jetbrains.com/statistics",

    # ── Adobe Creative Cloud ──

    "cc-api-data.adobe.io",
    "cc-api-data-stage.adobe.io",
    "geo2.adobe.com",
    "analytics.adobe.io",
    "sstats.adobe.com",

    # ── Atlassian ──

    "api-private.atlassian.com",
    "xid.atlassian.com",
    "as.atlassian.com",

    # ── Firebase / Google SDK ──

    "firebaselogging-pa.googleapis.com",
    "app-measurement.com",
    "firebase-settings.crashlytics.com",
    "firebaseinstallations.googleapis.com",
    "fcmregistrations.googleapis.com",
    "update.googleapis.com",

    # ── Apple ──

    "xp.apple.com",
    "metrics.icloud.com",
    "metrics.mzstatic.com",
    "diagnostics.apple.com",
    "iphonesubmissions.apple.com",
    "radarsubmissions.apple.com",

    # ── AWS SDK telemetry ──

    "fides-telemetry.us-east-1.amazonaws.com",
    "global.telemetry.aws.dev",

    # ── Stripe ──

    "m.stripe.com",
    "r.stripe.com",
    "q.stripe.com",

    # ── Twilio / SendGrid ──

    "eventgw.twilio.com",
    "tel.twilio.com",

    # ── Slack ──

    "telemetry.slack.com",
    "alog.files.slack.com",

    # ── GitHub (Copilot telemetry) ──

    "copilot-telemetry.githubusercontent.com",

    # ── LaunchDarkly ──

    "events.launchdarkly.com",
    "mobile.launchdarkly.com",
    "clientstream.launchdarkly.com",

    # ── Split.io ──

    "events.split.io",
    "telemetry.split.io",
    "sdk.split.io",

    # ── Elastic / ELK APM ──

    "apm.elastic.co",
    "telemetry.elastic.co",

    # ── Vercel / Next.js ──

    "vitals.vercel-insights.com",
    "va.vercel-scripts.com",
    "next-telemetry.vercel.sh",
    "telemetry.nextjs.org",

    # ── Netlify ──

    "netlify-rum.netlify.app",

    # ── Build tool / package manager telemetry ──

    "telemetry.yarnpkg.com",
    "telemetry.readthedocs.org",
})

# Default per-domain rate limits for well-known domains.
#
# These are a SAFETY NET — plugins enforce their own stricter per-API
# limits (IGDB 4 req/s, SteamGridDB 5 req/s, etc.).  NetworkManager
# limits must be generous enough to avoid double-throttling while still
# preventing runaway request storms.
_DEFAULT_DOMAIN_LIMITS: Dict[str, Dict[str, Any]] = {
    "api.steampowered.com": {"requests": 200, "window": 300},
    "store.steampowered.com": {"requests": 10, "window": 1},
    "steamcdn-a.akamaihd.net": {"requests": 20, "window": 1},
    "www.steamgriddb.com": {"requests": 15, "window": 1},
    "cdn2.steamgriddb.com": {"requests": 20, "window": 1},
    "api.igdb.com": {"requests": 10, "window": 1},
    "images.igdb.com": {"requests": 20, "window": 1},
    "www.pcgamingwiki.com": {"requests": 5, "window": 1},
    "www.protondb.com": {"requests": 15, "window": 1},
    "api.gog.com": {"requests": 15, "window": 1},
    "embed.gog.com": {"requests": 15, "window": 1},
    "catalog.gog.com": {"requests": 15, "window": 1},
    "www.gog.com": {"requests": 15, "window": 1},
    "menu.gog.com": {"requests": 15, "window": 1},
    "gog-statics.com": {"requests": 20, "window": 1},
    "images.gog-statics.com": {"requests": 20, "window": 1},
    "store-content.ak.epicgames.com": {"requests": 15, "window": 1},
    "graphql.epicgames.com": {"requests": 15, "window": 1},
    "cdn1.epicgames.com": {"requests": 20, "window": 1},
    "cdn2.epicgames.com": {"requests": 20, "window": 1},
}

# Fallback rate limit for unknown domains
_FALLBACK_RATE_LIMIT = {"requests": 10, "window": 1}


# ── Domain rate limiter ──────────────────────────────────────────────

class _DomainRateLimiter:
    """Thread-safe sliding-window rate limiter for a single domain."""

    __slots__ = ("max_requests", "window_seconds", "_request_times", "_lock")

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._request_times: List[float] = []
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Block until a request slot is available.

        Returns the number of seconds waited (0.0 if no wait needed).
        """
        with self._lock:
            now = time.monotonic()
            # Prune expired timestamps
            cutoff = now - self.window_seconds
            self._request_times = [
                t for t in self._request_times if t > cutoff
            ]

            if len(self._request_times) < self.max_requests:
                self._request_times.append(now)
                return 0.0

            # Must wait — oldest request defines when a slot opens
            oldest = self._request_times[0]
            wait_time = self.window_seconds - (now - oldest)
            if wait_time <= 0:
                self._request_times.append(now)
                return 0.0

        # Sleep outside the lock
        time.sleep(wait_time)

        with self._lock:
            self._request_times.append(time.monotonic())
        return wait_time


# ── Request statistics ───────────────────────────────────────────────

class _PluginStats:
    """Per-plugin request statistics."""

    __slots__ = ("_domain_counts", "_domain_bytes", "_domain_last_ts", "_lock")

    def __init__(self):
        self._domain_counts: Dict[str, int] = {}
        self._domain_bytes: Dict[str, int] = {}
        self._domain_last_ts: Dict[str, float] = {}
        self._lock = threading.Lock()

    def record(self, domain: str, response_bytes: int = 0) -> None:
        with self._lock:
            self._domain_counts[domain] = self._domain_counts.get(domain, 0) + 1
            self._domain_bytes[domain] = (
                self._domain_bytes.get(domain, 0) + response_bytes
            )
            self._domain_last_ts[domain] = time.time()

    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            result = {}
            for domain in self._domain_counts:
                result[domain] = {
                    "count": self._domain_counts[domain],
                    "bytes": self._domain_bytes.get(domain, 0),
                    "last_request": self._domain_last_ts.get(domain, 0),
                }
            return result

    def reset(self) -> None:
        with self._lock:
            self._domain_counts.clear()
            self._domain_bytes.clear()
            self._domain_last_ts.clear()


# ── Plugin registration ──────────────────────────────────────────────

class _PluginRegistration:
    """Holds per-plugin network configuration."""

    __slots__ = ("name", "allowed_domains", "session", "stats")

    def __init__(self, name: str, allowed_domains: Set[str]):
        self.name = name
        self.allowed_domains = allowed_domains
        self.session = _create_plugin_session()
        self.stats = _PluginStats()


def _create_plugin_session() -> requests.Session:
    """Create a requests.Session with default pooling and retry."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=20,
        max_retries=retry,
        pool_block=False,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ── NetworkManager ───────────────────────────────────────────────────

class DomainBlockedError(Exception):
    """Raised when a plugin tries to access a domain not in its allowlist."""
    pass


class OfflineError(Exception):
    """Raised when a request is attempted while offline."""
    pass


class NetworkManager:
    """Central hub for ALL network activity (plugin + core).

    Instantiated once by ``PluginManager`` and injected into the SDK
    registry. Plugins interact via ``PluginHttpClient``.
    """

    # Pseudo-plugin name for core HTTP requests (update checker, etc.)
    CORE_PLUGIN = "_core"

    def __init__(self):
        self._online = True
        self._plugins: Dict[str, _PluginRegistration] = {}
        self._rate_limiters: Dict[str, _DomainRateLimiter] = {}
        self._rate_limiter_lock = threading.Lock()
        self._network_monitor = None  # Set via set_network_monitor()

        # Core image session (not subject to plugin allowlists)
        self._image_session: Optional[requests.Session] = None

        # Image download statistics (key "_images" in get_all_stats())
        self._image_stats = _PluginStats()

        # Register core pseudo-plugin for update checker, etc.
        self.register_plugin(
            self.CORE_PLUGIN,
            allowed_domains=[
                "luducat-api-proxy.luducat-cloudflare.workers.dev",
            ],
        )

        logger.debug("NetworkManager initialized")

    # ── Online/offline ───────────────────────────────────────────────

    @property
    def is_online(self) -> bool:
        """Current network mode."""
        if self._network_monitor is not None:
            return self._network_monitor.is_online
        return self._online

    def set_online(self, online: bool) -> None:
        """Set online/offline mode. Prefer using NetworkMonitor directly."""
        self._online = online
        if self._network_monitor is not None:
            self._network_monitor.set_mode(online)

    def set_network_monitor(self, monitor) -> None:
        """Integrate with the existing NetworkMonitor singleton."""
        self._network_monitor = monitor

    # ── Plugin registration ──────────────────────────────────────────

    def register_plugin(
        self,
        plugin_name: str,
        allowed_domains: List[str],
        rate_limits: Optional[Dict[str, Dict]] = None,
    ) -> None:
        """Register a plugin with its network configuration.

        Args:
            plugin_name: Plugin identifier
            allowed_domains: List of domains the plugin may access
            rate_limits: Optional per-domain rate limits override.
                         Format: ``{"domain": {"requests": N, "window": S}}``
        """
        domain_set = set(allowed_domains)
        self._plugins[plugin_name] = _PluginRegistration(
            name=plugin_name,
            allowed_domains=domain_set,
        )

        # Register custom rate limits from plugin.json
        if rate_limits:
            for domain, config in rate_limits.items():
                self._ensure_rate_limiter(
                    domain,
                    config.get("requests", 10),
                    config.get("window", 1),
                )

        logger.debug(
            f"Registered plugin '{plugin_name}' with "
            f"{len(domain_set)} allowed domains"
        )

    def unregister_plugin(self, plugin_name: str) -> None:
        """Remove a plugin's network registration."""
        reg = self._plugins.pop(plugin_name, None)
        if reg and reg.session:
            reg.session.close()

    # ── Rate limiting ────────────────────────────────────────────────

    def _ensure_rate_limiter(
        self, domain: str, max_requests: int, window_seconds: float
    ) -> _DomainRateLimiter:
        """Get or create a rate limiter for a domain."""
        with self._rate_limiter_lock:
            if domain not in self._rate_limiters:
                self._rate_limiters[domain] = _DomainRateLimiter(
                    max_requests, window_seconds
                )
            return self._rate_limiters[domain]

    def _get_rate_limiter(self, domain: str) -> _DomainRateLimiter:
        """Get the rate limiter for a domain, creating from defaults if needed."""
        with self._rate_limiter_lock:
            if domain in self._rate_limiters:
                return self._rate_limiters[domain]

        # Check default limits
        config = _DEFAULT_DOMAIN_LIMITS.get(domain, _FALLBACK_RATE_LIMIT)
        return self._ensure_rate_limiter(
            domain, config["requests"], config["window"]
        )

    def wait_for_rate_limit(self, domain: str) -> float:
        """Block until a request slot is available for the given domain.

        Returns seconds waited.
        """
        limiter = self._get_rate_limiter(domain)
        return limiter.wait()

    # ── Domain validation ────────────────────────────────────────────

    def _check_domain(self, plugin_name: str, url: str) -> str:
        """Validate that the plugin may access the URL's domain.

        Returns the domain string on success.
        Raises ``DomainBlockedError`` on violation.
        """
        parsed = urlparse(url)
        domain = parsed.hostname or ""

        # Analytics blocklist — always blocked
        for blocked in _ANALYTICS_BLOCKLIST:
            if domain == blocked or domain.endswith("." + blocked):
                logger.warning(
                    f"Plugin '{plugin_name}' blocked from analytics domain: {domain}"
                )
                raise DomainBlockedError(
                    f"Request to analytics/tracking domain '{domain}' blocked"
                )

        reg = self._plugins.get(plugin_name)
        if reg is None:
            # Unregistered plugin — allow (bundled plugins during transition)
            return domain

        if not reg.allowed_domains:
            # Empty allowlist means unrestricted (bundled plugins)
            return domain

        # Check if domain matches any allowed domain
        for allowed in reg.allowed_domains:
            if domain == allowed or domain.endswith("." + allowed):
                return domain

        logger.warning(
            f"Plugin '{plugin_name}' blocked from domain '{domain}' "
            f"(not in allowed: {reg.allowed_domains})"
        )
        raise DomainBlockedError(
            f"Plugin '{plugin_name}' is not allowed to access '{domain}'"
        )

    # ── Default headers ──────────────────────────────────────────────

    def get_default_headers(self, url: str) -> Dict[str, str]:
        """Build browser-like headers for a request URL.

        Includes Origin/Referer based on the request domain.
        """
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.hostname}"
        headers = dict(_DEFAULT_BROWSER_HEADERS)
        headers["Origin"] = base
        headers["Referer"] = base + "/"
        return headers

    # ── Request execution ────────────────────────────────────────────

    def execute_request(
        self,
        plugin_name: str,
        method: str,
        url: str,
        **kwargs,
    ) -> requests.Response:
        """Execute an HTTP request on behalf of a plugin.

        Enforces: online check, domain allowlist, rate limiting,
        default headers. Records statistics.

        Args:
            plugin_name: Plugin identifier
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            **kwargs: Passed to ``requests.Session.request()``

        Returns:
            ``requests.Response``

        Raises:
            OfflineError: App is in offline mode
            DomainBlockedError: Domain not in plugin's allowlist
        """
        if not self.is_online:
            raise OfflineError("Application is in offline mode")

        domain = self._check_domain(plugin_name, url)

        # Rate limit
        self.wait_for_rate_limit(domain)

        # Merge default headers (plugin-provided headers override)
        default_headers = self.get_default_headers(url)
        if "headers" in kwargs:
            default_headers.update(kwargs["headers"])
        kwargs["headers"] = default_headers

        # Ensure timeout
        if "timeout" not in kwargs:
            kwargs["timeout"] = 30

        # Get plugin session
        reg = self._plugins.get(plugin_name)
        session = reg.session if reg else _create_plugin_session()

        try:
            response = session.request(method, url, **kwargs)
            # Record stats — use Content-Length header for streaming
            # requests to avoid forcing the full body into memory.
            if kwargs.get("stream"):
                content_length = int(
                    response.headers.get("Content-Length", 0)
                )
            else:
                content_length = (
                    len(response.content) if response.content else 0
                )
            if reg:
                reg.stats.record(domain, content_length)
            return response
        except requests.RequestException:
            if reg:
                reg.stats.record(domain, 0)
            raise

    def get_plugin_session(self, plugin_name: str) -> requests.Session:
        """Get the raw session for a plugin (for advanced use).

        Plugins should prefer ``execute_request()`` for automatic
        rate limiting and domain checking.
        """
        reg = self._plugins.get(plugin_name)
        if reg:
            return reg.session
        return _create_plugin_session()

    # ── Image cache integration ──────────────────────────────────────

    def get_image_session(self) -> requests.Session:
        """Get the shared session for image cache downloads.

        NOT subject to plugin domain allowlists (core functionality).
        Uses the same pooling/retry config as plugin sessions.
        """
        if self._image_session is None:
            self._image_session = _create_plugin_session()
        return self._image_session

    # ── External stat recording ─────────────────────────────────────

    def record_request(
        self, plugin_name: str, domain: str, response_bytes: int = 0
    ) -> None:
        """Record a request made outside ``execute_request()``.

        Escape hatch for async code (e.g. future third-party aiohttp
        plugins) that cannot route through ``execute_request()`` but
        still wants its traffic counted.
        """
        reg = self._plugins.get(plugin_name)
        if reg:
            reg.stats.record(domain, response_bytes)

    def record_image_request(
        self, domain: str, response_bytes: int = 0
    ) -> None:
        """Record an image download for the ``_images`` stats row."""
        self._image_stats.record(domain, response_bytes)

    # ── Statistics ───────────────────────────────────────────────────

    def get_plugin_stats(self, plugin_name: str) -> Dict[str, Dict[str, Any]]:
        """Get request statistics for a plugin.

        Returns:
            Dict mapping domain → {count, bytes, last_request}
        """
        reg = self._plugins.get(plugin_name)
        if reg:
            return reg.stats.get_stats()
        return {}

    def get_all_stats(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Get request statistics for all plugins.

        Returns:
            Dict mapping plugin_name → domain → {count, bytes, last_request}.
            Image cache stats are included under key ``"_images"``.
        """
        result = {
            name: reg.stats.get_stats()
            for name, reg in self._plugins.items()
        }
        image_stats = self._image_stats.get_stats()
        if image_stats:
            result["_images"] = image_stats
        return result

    def reset_all_stats(self) -> None:
        """Reset request statistics for all plugins and image cache."""
        for reg in self._plugins.values():
            reg.stats.reset()
        self._image_stats.reset()

    # ── Cleanup ──────────────────────────────────────────────────────

    def close(self) -> None:
        """Close all sessions and release resources."""
        for reg in self._plugins.values():
            if reg.session:
                reg.session.close()
        self._plugins.clear()

        if self._image_session:
            self._image_session.close()
            self._image_session = None

        logger.debug("NetworkManager closed")


# ── Module-level singleton ───────────────────────────────────────────

_manager: Optional[NetworkManager] = None


def get_network_manager() -> NetworkManager:
    """Get or create the global NetworkManager instance."""
    global _manager
    if _manager is None:
        _manager = NetworkManager()
    return _manager


def reset_network_manager() -> None:
    """Reset the singleton (for testing)."""
    global _manager
    if _manager is not None:
        _manager.close()
    _manager = None
