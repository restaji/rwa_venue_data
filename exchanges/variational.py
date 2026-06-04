#!/usr/bin/env python3
"""
exchanges/variational.py - Variational Omni exchange integration.

Covers: live size-adjusted quotes (bid/ask + implied slippage), taker fee, and
max leverage. Oracle/quote-based RWA perps – no traditional orderbook.

Public APIs used (Cloudflare-fronted, accessed via curl_cffi Chrome TLS
impersonation rather than a spoofed User-Agent):
  Quote:  POST https://omni.variational.io/api/quotes/simple
  Config: leverage table + taker fee parsed live from the app JS bundle

Instruments are RWA commodity perps (XAU, XAG, WTI, BRENT) settled in USDC.
"""
from __future__ import annotations

import collections
import logging
import re
import time
from typing import Dict, Optional
from urllib.parse import urljoin

from curl_cffi import requests  # type: ignore[import-not-found]

log = logging.getLogger(__name__)


class VariationalAPI:
    """Variational Omni live quote + leverage fetcher (browserless)."""

    BASE = "https://omni.variational.io"
    QUOTE_URL = f"{BASE}/api/quotes/simple"
    IMPERSONATE = "chrome"
    SETTLEMENT_ASSET = "USDC"

    VARIATIONAL_INSTRUMENTS: Dict[str, Dict] = {
        "XAU":   {"underlying": "XAU", "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # Gold
        "XAG":   {"underlying": "XAG", "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # Silver
        "WTI":   {"underlying": "CL",  "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # WTI Crude
        "BRENT": {"underlying": "BZ",  "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # Brent Crude
    }

    # asset_key -> Variational's own ticker, shown in the comparator. Populated
    # from VARIATIONAL_INSTRUMENTS below so that map is the single source of truth.
    VARIATIONAL_SYMBOLS: Dict[str, str] = {}

    # Regex matching dynamic JS imports inside the app bundle.
    _IMPORT_RE = re.compile(r"""(?:from|import\()\s*["']([^"']+\.js)["']""")

    def __init__(self, cache_ttl: int = 30, config_ttl: int = 600):
        self._cache_ttl = cache_ttl
        self._config_ttl = config_ttl
        # underlying -> (mark, ts) reference mark used only to size the qty.
        self._mark_cache: Dict[str, tuple] = {}
        # cached app config (leverage table + taker fee) parsed from the bundle.
        self._config_cache = {"data": None, "ts": 0.0}

    # ------------------------------------------------------------------
    # Low-level HTTP (Chrome TLS impersonation)
    # ------------------------------------------------------------------

    def _headers(self, underlying: str) -> dict:
        return {"origin": self.BASE, "referer": f"{self.BASE}/perpetual/{underlying}"}

    def _get(self, url: str, underlying: str = "XAU") -> str:
        return requests.get(
            url, impersonate=self.IMPERSONATE, timeout=20, headers=self._headers(underlying)
        ).text

    def fetch_quote(self, asset_key: str, qty) -> Optional[Dict]:
        """POST a single size-adjusted quote for ``asset_key`` at ``qty`` base
        units. Returns the raw JSON dict, or None on failure."""
        spec = self.VARIATIONAL_INSTRUMENTS.get(asset_key.upper())
        if not spec:
            return None

        underlying = spec["underlying"]
        instrument = {"settlement_asset": self.SETTLEMENT_ASSET, **spec}
        body = {"instrument": instrument, "qty": str(qty)}
        try:
            r = requests.post(
                self.QUOTE_URL,
                json=body,
                impersonate=self.IMPERSONATE,
                timeout=20,
                headers=self._headers(underlying),
            )
            if r.status_code != 200:
                log.warning(
                    "Variational: quote %s qty=%s failed HTTP %s: %s",
                    underlying, qty, r.status_code, r.text[:200],
                )
                return None
            return r.json()
        except Exception:
            log.exception("Variational: quote request error for %s", underlying)
            return None

    # ------------------------------------------------------------------
    # App config (leverage + taker fee), parsed live from the app bundle
    # ------------------------------------------------------------------

    @staticmethod
    def _find_export_local(text: str, alias: str) -> Optional[str]:
        m = re.search(r"(\w+) as " + re.escape(alias) + r"\b", text)
        return m.group(1) if m else None

    def _find_import_source(self, text: str, alias: str, current_url: str):
        """Find the module URL that exports ``<something> as alias``."""
        for m in re.finditer(r'import\{([^}]*)\}from"([^"]+)"', text):
            local = self._find_export_local(m.group(1), alias)
            if local:
                return local, urljoin(current_url, m.group(2))
        return None, None

    def _parse_leverage(self, text: str, url: str) -> Optional[Dict]:
        """Extract the crypto-default + per-equity max-leverage map from one
        module's source, or None if this module doesn't hold it."""
        m = re.search(r"r\.max_leverage;if\(t\)return t\}return (\w+)\}", text)
        if not m:
            return None
        default_var = m.group(1)

        equity: Dict[str, float] = {}
        for em in re.finditer(r'(\w+):\{name:"[^"]*"[\s\S]*?max_leverage:"(\d+)"', text):
            equity[em.group(1).upper()] = float(em.group(2))

        crypto = None
        loc = re.search(re.escape(default_var) + r'="?(\d+(?:\.\d+)?)"?', text)
        if loc:
            crypto = float(loc.group(1))
        else:
            exp_name, src_url = self._find_import_source(text, default_var, url)
            if src_url:
                st = self._get(src_url)
                localname = self._find_export_local(st, exp_name)
                if localname:
                    vm = re.search(re.escape(localname) + r'="?(\d+(?:\.\d+)?)"?', st)
                    if vm:
                        crypto = float(vm.group(1))

        if crypto is None:
            return None
        return {"crypto_default": crypto, "equity": equity}

    def get_app_config(self) -> Dict:
        """Walk the JS module graph from the page entry points and extract the
        live app config: the max-leverage table (crypto default + per-equity
        map) and the futures taker fee. Discovered dynamically so it survives
        redeploys; the two values live in different modules so we keep crawling
        until both are found."""
        html = self._get(f"{self.BASE}/perpetual/XAU")
        entries = re.findall(r"/_app/immutable/entry/[A-Za-z0-9_.-]+\.js", html)

        seen = set()
        queue = collections.deque(self.BASE + e for e in entries)

        leverage: Optional[Dict] = None
        taker_fee_bps: Optional[float] = None

        while queue and len(seen) < 400:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                text = self._get(url)
            except Exception:
                continue

            if leverage is None:
                leverage = self._parse_leverage(text, url)

            if taker_fee_bps is None:
                fm = re.search(r'futures_taker_fee:"?(\d+(?:\.\d+)?)"?', text)
                if fm:
                    # Config stores the fee as a rate fraction (e.g. 0.0004 = 4
                    # bps); convert to bps. Currently 0 on Omni (spread-only).
                    taker_fee_bps = float(fm.group(1)) * 10000.0

            if leverage is not None and taker_fee_bps is not None:
                break

            for rel in self._IMPORT_RE.findall(text):
                queue.append(urljoin(url, rel))

        if leverage is None and taker_fee_bps is None:
            raise RuntimeError("could not locate app config in the bundle")

        return {
            "crypto_default": leverage["crypto_default"] if leverage else None,
            "equity":         leverage["equity"] if leverage else {},
            "taker_fee_bps":  taker_fee_bps,
        }

    def get_app_config_cached(self) -> Optional[Dict]:
        """Memoised ``get_app_config`` (TTL = ``config_ttl``)."""
        now = time.time()
        cache = self._config_cache
        if cache["data"] is None or now - cache["ts"] > self._config_ttl:
            try:
                cache["data"] = self.get_app_config()
                cache["ts"] = now
            except Exception:
                log.exception("Variational: failed to parse app config")
        return cache["data"]

    def max_leverage_for(self, underlying: str) -> Optional[float]:
        """Max leverage the UI shows for ``underlying`` (per-equity override,
        else the live crypto default). Returns None if the live config can't be
        parsed — we never invent a number."""
        cfg = self.get_app_config_cached()
        if not cfg or cfg.get("crypto_default") is None:
            return None
        return cfg["equity"].get(underlying.upper(), cfg["crypto_default"])

    def taker_fee_bps(self) -> Optional[float]:
        """Live futures taker fee (bps) parsed from the app config, or None if
        it can't be read."""
        cfg = self.get_app_config_cached()
        return cfg.get("taker_fee_bps") if cfg else None

    # ------------------------------------------------------------------
    # Symbol resolution / qty sizing
    # ------------------------------------------------------------------

    def _display_symbol(self, asset_key: str) -> str:
        return self.VARIATIONAL_SYMBOLS.get(asset_key.upper(), asset_key.upper())

    def _reference_mark(self, asset_key: str) -> Optional[float]:
        """Return a (cached) reference mark used to convert a USD notional into
        a base-unit qty. Primed with a tiny throwaway quote on a cold cache."""
        key = asset_key.upper()
        cached = self._mark_cache.get(key)
        if cached and time.time() - cached[1] < self._cache_ttl:
            return cached[0]

        q = self.fetch_quote(key, 1)
        if not q:
            return cached[0] if cached else None
        try:
            mark = float(q["mark_price"])
        except (KeyError, TypeError, ValueError):
            return cached[0] if cached else None
        if mark > 0:
            self._mark_cache[key] = (mark, time.time())
            return mark
        return cached[0] if cached else None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def calculate_cost(
        self,
        asset_key: str,
        order_size_usd: float,
        is_long: bool = True,
    ) -> Optional[Dict]:
        """
        Return Variational execution stats for an asset and clip size.

        Fees are structurally zero on Omni, so the full round-trip cost is just
        the size-adjusted spread. We request a real, live quote sized to
        ``order_size_usd`` (qty = notional / mark) and measure slippage per side
        from the quote midpoint ((bid + ask) / 2) — the direct analog of the
        orderbook venues' (best_bid + best_ask) / 2 mid. No bucketing, no
        interpolation: the OLP prices the exact clip size we ask for.
        """
        key = asset_key.upper()
        if key not in self.VARIATIONAL_INSTRUMENTS:
            return None
        display = self._display_symbol(key)

        mark_ref = self._reference_mark(key)
        if not mark_ref or mark_ref <= 0:
            return None

        qty = order_size_usd / mark_ref
        if qty <= 0:
            return None

        quote = self.fetch_quote(key, qty)
        if not quote:
            return None

        try:
            bid = float(quote["bid"])
            ask = float(quote["ask"])
            mark_price = float(quote["mark_price"])
        except (KeyError, TypeError, ValueError):
            return None

        try:
            index_price = float(quote["index_price"])
        except (KeyError, TypeError, ValueError):
            index_price = None

        try:
            filled_qty = float(quote.get("qty", qty))
        except (TypeError, ValueError):
            filled_qty = qty

        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None

        # Per-side slippage from the live quote midpoint.
        buy_slip_bps  = (ask - mid) / mid * 10000.0   # cost to open/close a long buy
        sell_slip_bps = (mid - bid) / mid * 10000.0   # cost to open/close a short sell
        avg_slip_bps  = (buy_slip_bps + sell_slip_bps) / 2.0
        spread_bps    = (ask - bid) / mid * 10000.0

        opening_slip = buy_slip_bps  if is_long else sell_slip_bps
        closing_slip = sell_slip_bps if is_long else buy_slip_bps

        # Live app config: max leverage + futures taker fee. The fee is charged
        # on both open and close; you always take against the OLP, so there's no
        # maker side. Fall back to 0.0 (Omni's documented spread-only model) if
        # the config can't be parsed, so the venue still ranks.
        cfg = self.get_app_config_cached() or {}
        max_lev = (
            cfg["equity"].get(display.upper(), cfg["crypto_default"])
            if cfg.get("crypto_default") is not None else None
        )
        fee_bps = cfg.get("taker_fee_bps")
        if fee_bps is None:
            fee_bps = 0.0

        # Round-trip cost = full spread + open fee + close fee.
        total_cost_bps = buy_slip_bps + sell_slip_bps + 2.0 * fee_bps

        # The OLP can quote enormous sizes; treat the quote as fully filled
        # unless it explicitly caps qty below what we asked for.
        filled = True
        limits = quote.get("qty_limits") or {}
        try:
            max_qty = min(
                float(limits["bid"]["max_qty"]),
                float(limits["ask"]["max_qty"]),
            )
            filled = qty <= max_qty
        except (KeyError, TypeError, ValueError):
            pass

        filled_usd = filled_qty * mark_price
        notional   = qty * mark_price

        return {
            "symbol":            display,        # asset we're comparing (e.g. XAU)
            "name":              display,
            "underlying":        display,
            "max_leverage":      max_lev,
            "executed":          True if filled else "PARTIAL",
            "filled":            filled,
            "mid_price":         mid,            # live quote midpoint
            "mark_price":        mark_price,     # oracle mark (reference only)
            "index_price":       index_price,
            # --- fees: live futures taker fee (0 on Omni today; spread-only) ---
            "open_fee_bps":      fee_bps,
            "close_fee_bps":     fee_bps,
            "fee_bps":           fee_bps,
            "maker_fee_bps":     fee_bps,   # no maker side — always taker vs OLP
            # --- size-adjusted, direction-aware spread / slippage ---
            "base_spread_bps":   spread_bps,     # live spread at this clip size
            "slippage_bps":      avg_slip_bps,   # per-side avg; comparator doubles it
            "buy_slippage_bps":  buy_slip_bps,
            "sell_slippage_bps": sell_slip_bps,
            "opening_slippage_bps": opening_slip,
            "closing_slippage_bps": closing_slip,
            "slippage_type":     "opening_closing",
            "total_cost_bps":    total_cost_bps,
            # --- sizing detail ---
            "order_size_usd":    order_size_usd,
            "filled_usd":        filled_usd,
            "unfilled_usd":      max(0.0, notional - filled_usd),
            "notional":          notional,
            "qty":               filled_qty,
            "is_long":           is_long,
            # --- raw quote prices ---
            "bid":               bid,
            "ask":               ask,
            "quote_id":          quote.get("quote_id"),
            "quotes_updated_at": quote.get("timestamp"),
            "timestamp":         time.time(),
        }


# Single source of truth: derive the comparator's display tickers from the
# instrument map so adding a pair above is the only edit needed.
VariationalAPI.VARIATIONAL_SYMBOLS = {
    key: spec["underlying"] for key, spec in VariationalAPI.VARIATIONAL_INSTRUMENTS.items()
}
