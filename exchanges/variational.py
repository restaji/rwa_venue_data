#!/usr/bin/env python3
"""
exchanges/variational.py - Variational Omni exchange integration.

Covers: live size-adjusted quotes (bid/ask + implied slippage), taker fee, and
max leverage. Oracle/quote-based RWA perps – no traditional orderbook.

Public APIs used (Cloudflare-fronted, accessed via curl_cffi Chrome TLS
impersonation rather than a spoofed User-Agent):
  Quote:   POST https://omni.variational.io/api/quotes/simple
  Assets:  GET  https://omni.variational.io/api/metadata/supported_assets
  Config:  GET  https://omni.variational.io/api/metadata/config  (taker fee)
           plus the app JS bundle for the UI's max-leverage helper

Instruments are RWA perps (commodities, pre-IPO equity, listed equity, ETFs)
settled in USDC.
"""
from __future__ import annotations

import collections
import logging
import re
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin

from curl_cffi import requests  # type: ignore[import-not-found]

from models import floor_leverage

log = logging.getLogger(__name__)


class VariationalAPI:
    """Variational Omni live quote + leverage fetcher (browserless)."""

    BASE = "https://omni.variational.io"
    QUOTE_URL = f"{BASE}/api/quotes/simple"
    SUPPORTED_ASSETS_URL = f"{BASE}/api/metadata/supported_assets"
    CONFIG_URL = f"{BASE}/api/metadata/config"
    IMPERSONATE = "chrome"
    SETTLEMENT_ASSET = "USDC"

    VARIATIONAL_INSTRUMENTS: Dict[str, Dict] = {
        # Commodities
        "XAU":    {"underlying": "XAU",    "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # Gold
        "XAG":    {"underlying": "XAG",    "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # Silver
        "WTI":    {"underlying": "CL",     "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # WTI Crude
        "BRENT":  {"underlying": "BZ",     "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # Brent Crude
        "NATGAS": {"underlying": "NATGAS", "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # Natural Gas
        "COPPER": {"underlying": "COPPER", "instrument_type": "perpetual_rwa_future", "kind": "commodity"},  # Copper
        # Pre-IPO / private equity
        "SPCX":      {"underlying": "SPCX",      "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # SpaceX
        "OPENAI":    {"underlying": "OPENAI",    "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # OpenAI
        "ANTHROPIC": {"underlying": "ANTHROPIC", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Anthropic
        "QNTX":      {"underlying": "QNTX",      "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Quantinuum
        # Listed equity
        "AMD":  {"underlying": "AMD",  "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Advanced Micro Devices
        "CBRS": {"underlying": "CBRS", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Celsius Resources
        "COIN": {"underlying": "COIN", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Coinbase
        "CRCL": {"underlying": "CRCL", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Circle
        "INTC": {"underlying": "INTC", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Intel
        "LITE": {"underlying": "LITE", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Lumentum
        "MRVL": {"underlying": "MRVL", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Marvell
        "MSTR": {"underlying": "MSTR", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # MicroStrategy
        "MU":   {"underlying": "MU",   "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Micron
        "NVDA": {"underlying": "NVDA", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # NVIDIA
        "QCOM": {"underlying": "QCOM", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Qualcomm
        "RKLB": {"underlying": "RKLB", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Rocket Lab
        "SNDK": {"underlying": "SNDK", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Sandisk
        "TSLA": {"underlying": "TSLA", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # Tesla
        "TSM":  {"underlying": "TSM",  "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # TSMC
        "USAR": {"underlying": "USAR", "instrument_type": "perpetual_rwa_future", "kind": "equity"},  # USA Rare Earth
        # ETFs
        "US100": {"underlying": "QQQ",   "instrument_type": "perpetual_rwa_future", "kind": "ETF"},  # Nasdaq-100 (Variational ticker: QQQ)
        "US500": {"underlying": "US500", "instrument_type": "perpetual_rwa_future", "kind": "ETF"},  # S&P 500
        "DRAM": {"underlying": "DRAM",  "instrument_type": "perpetual_rwa_future", "kind": "ETF"},  # Dynamic Random Access Memory ETF
        "EWJ":  {"underlying": "EWJ",   "instrument_type": "perpetual_rwa_future", "kind": "ETF"},  # iShares MSCI Japan ETF
        "EWY":  {"underlying": "EWY",   "instrument_type": "perpetual_rwa_future", "kind": "ETF"},  # iShares MSCI South Korea ETF
        "SOXL": {"underlying": "SOXL",  "instrument_type": "perpetual_rwa_future", "kind": "ETF"},  # Direxion Daily Semiconductor Bull 3X
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

    def _headers(self, underlying: str = "XAU") -> dict:
        return {"origin": self.BASE, "referer": f"{self.BASE}/perpetual/{underlying}"}

    def _get(self, url: str, underlying: str = "XAU") -> str:
        return requests.get(
            url, impersonate=self.IMPERSONATE, timeout=20, headers=self._headers(underlying)
        ).text

    def _get_json(self, url: str, underlying: str = "XAU") -> Optional[Dict]:
        try:
            r = requests.get(
                url, impersonate=self.IMPERSONATE, timeout=20, headers=self._headers(underlying)
            )
            if r.status_code != 200:
                log.warning("Variational: GET %s failed HTTP %s", url, r.status_code)
                return None
            return r.json()
        except (TypeError, ValueError, KeyError, AttributeError, OSError, RuntimeError):
            log.exception("Variational: GET %s error", url)
            return None

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
        except (TypeError, ValueError, KeyError, AttributeError, OSError, RuntimeError):
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

    # Sub-patterns for walking minified object/array literals. ``_INNER`` is the
    # body of a single object (permitting ``${...}`` template substitutions),
    # ``_OBJ`` a whole ``{...}`` object, and ``_ARR`` an array that may itself
    # contain such objects (one level deep — enough for the tradfi tables).
    _INNER = r'(?:[^{}]|\$\{[^}]*\})*'
    _OBJ   = r'\{' + _INNER + r'\}'
    _ARR   = r'\[(?:[^\[\]{}]|\$\{[^}]*\}|' + _OBJ + r')*\]'

    @classmethod
    def _parse_equity(cls, text: str) -> Dict[str, float]:
        equity: Dict[str, float] = {}

        # ``Ge``'s default leverage, applied to any bare-string ticker.
        gm = re.search(
            r'typeof \w+=="string"\)return\[\w+,\{' + cls._INNER + r'max_leverage:"(\d+)"',
            text,
        )
        ge_default = float(gm.group(1)) if gm else None

        # Each array fed to ``Ge``, e.g. ``...Ge(Xa,"etf","svg")``.
        for var in re.findall(r'\.\.\.\w+\((\w+),"[a-z]+","[a-z]+"\)', text):
            am = re.search(re.escape(var) + r'=(' + cls._ARR + r')', text)
            if not am:
                continue
            content = am.group(1)
            # Objects: explicit symbol, with an optional per-symbol override.
            for om in re.finditer(cls._OBJ, content):
                obj = om.group(0)
                sm = re.search(r'symbol:"([^"]+)"', obj)
                if not sm:
                    continue
                lm = re.search(r'max_leverage:"(\d+)"', obj)
                if lm:
                    equity[sm.group(1).upper()] = float(lm.group(1))
                elif ge_default is not None:
                    equity[sm.group(1).upper()] = ge_default
            # Remaining bare "TICKER" strings inherit Ge's default.
            if ge_default is not None:
                for bm in re.finditer(r'"([^"]+)"', re.sub(cls._OBJ, '', content)):
                    equity.setdefault(bm.group(1).upper(), ge_default)

        # Legacy fallback: standalone ``TICKER:{name:"…",max_leverage:"N"}`` shape.
        for em in re.finditer(r'(\w+):\{name:"[^"]*"' + cls._INNER + r'max_leverage:"(\d+)"', text):
            equity.setdefault(em.group(1).upper(), float(em.group(2)))

        return equity

    def _resolve_numeric_const(self, text: str, var: str, url: str) -> Optional[float]:
        """Resolve a minified identifier to a numeric literal, following a
        single import hop when the constant lives in another chunk."""
        loc = re.search(re.escape(var) + r'="?(\d+(?:\.\d+)?)"?', text)
        if loc:
            return float(loc.group(1))
        exp_name, src_url = self._find_import_source(text, var, url)
        if src_url:
            st = self._get(src_url)
            localname = self._find_export_local(st, exp_name)
            if localname:
                vm = re.search(re.escape(localname) + r'="?(\d+(?:\.\d+)?)"?', st)
                if vm:
                    return float(vm.group(1))
        return None

    def _parse_leverage_defaults(
        self, text: str, url: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """Extract (tradfi_default, crypto_default) from the UI's max-leverage
        helper in this module, or (None, None) if it isn't here.

        Current Omni helper (var names drift across redeploys)::

            <t>.max_leverage;if(<r>)return <r>}return <e>&&<ts>(<e>)?<Lr>:<Fr>}

        ``ts(e)`` is ``!!e.asset_class`` — any RWA with an asset_class (equity,
        commodity, ETF, …) gets the tradfi cap; crypto perps get the default.
        """
        m = re.search(
            r"\w+\.max_leverage;if\((\w+)\)return \1\}return \w+&&\w+\(\w+\)\?(\w+):(\w+)\}",
            text,
        )
        if m:
            tradfi = self._resolve_numeric_const(text, m.group(2), url)
            crypto = self._resolve_numeric_const(text, m.group(3), url)
            return tradfi, crypto

        # Legacy: <x>.max_leverage;if(<v>)return <v>}return <default>}
        m = re.search(r"\w+\.max_leverage;if\((\w+)\)return \1\}return (\w+)\}", text)
        if m:
            return None, self._resolve_numeric_const(text, m.group(2), url)
        return None, None

    def _fetch_asset_class_map(self) -> Dict[str, str]:
        """Ticker → asset_class for RWAs (equity/commodity/etf). Crypto perps
        omit the field; the UI treats a truthy asset_class as tradfi leverage."""
        data = self._get_json(self.SUPPORTED_ASSETS_URL) or {}
        out: Dict[str, str] = {}
        for ticker, recs in data.items():
            rec = recs[0] if isinstance(recs, list) and recs else recs
            if isinstance(rec, dict) and rec.get("asset_class"):
                out[str(ticker).upper()] = str(rec["asset_class"])
        return out

    def _fetch_taker_fee_bps(self) -> Optional[float]:
        data = self._get_json(self.CONFIG_URL) or {}
        raw = data.get("futures_taker_fee")
        if raw is None:
            return None
        try:
            return float(raw) * 10000.0
        except (TypeError, ValueError):
            return None

    def _is_rwa_underlying(self, symbol: str) -> bool:
        """True if ``symbol`` is one of our known RWA underlyings (all of
        VARIATIONAL_INSTRUMENTS). Used when the supported-assets API is down."""
        s = symbol.upper()
        if s in self.VARIATIONAL_INSTRUMENTS:
            return True
        return any(spec["underlying"].upper() == s for spec in self.VARIATIONAL_INSTRUMENTS.values())

    def get_app_config(self) -> Dict:
        """Live app config matching the Omni UI's max-leverage helper:

        * per-symbol ``max_leverage`` override from the tradfi table (rare)
        * otherwise tradfi cap if the market has ``asset_class``, else crypto default
        * futures taker fee from ``/api/metadata/config`` (JS bundle as fallback)

        Defaults are discovered from the JS bundle so they survive redeploys.
        """
        taker_fee_bps = self._fetch_taker_fee_bps()
        asset_class = self._fetch_asset_class_map()

        html = self._get(f"{self.BASE}/perpetual/XAU")
        entries = re.findall(r"/_app/immutable/entry/[A-Za-z0-9_.-]+\.js", html)

        seen = set()
        queue = collections.deque(self.BASE + e for e in entries)

        crypto_default: Optional[float] = None
        tradfi_default: Optional[float] = None
        equity: Dict[str, float] = {}

        while queue and len(seen) < 400:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                text = self._get(url)
            except (TypeError, ValueError, KeyError, AttributeError, OSError, RuntimeError):
                continue

            for ticker, lev in self._parse_equity(text).items():
                equity.setdefault(ticker, lev)

            if crypto_default is None or tradfi_default is None:
                parsed_tradfi, parsed_crypto = self._parse_leverage_defaults(text, url)
                if tradfi_default is None and parsed_tradfi is not None:
                    tradfi_default = parsed_tradfi
                if crypto_default is None and parsed_crypto is not None:
                    crypto_default = parsed_crypto

            if taker_fee_bps is None:
                fm = re.search(r'futures_taker_fee:"?(\d+(?:\.\d+)?)"?', text)
                if fm:
                    # Config stores the fee as a rate fraction (e.g. 0.0004 = 4
                    # bps); convert to bps. Currently 0 on Omni (spread-only).
                    taker_fee_bps = float(fm.group(1)) * 10000.0

            have_defaults = crypto_default is not None and (
                tradfi_default is not None or equity
            )
            if have_defaults and taker_fee_bps is not None:
                break

            for rel in self._IMPORT_RE.findall(text):
                queue.append(urljoin(url, rel))

        if (
            crypto_default is None
            and tradfi_default is None
            and taker_fee_bps is None
            and not equity
        ):
            raise RuntimeError("could not locate app config in the bundle")

        return {
            "crypto_default": crypto_default,
            "tradfi_default": tradfi_default,
            "equity":         equity,
            "asset_class":    asset_class,
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
            except (TypeError, ValueError, KeyError, AttributeError, OSError, RuntimeError):
                log.exception("Variational: failed to parse app config")
        return cache["data"]

    def _max_leverage_from_config(self, symbol: str, cfg: Dict) -> Optional[float]:
        """Mirror the Omni UI helper: per-symbol override, else tradfi cap if
        the market has ``asset_class``, else the crypto default."""
        symbol = symbol.upper()
        equity = cfg.get("equity") or {}
        if symbol in equity:
            return floor_leverage(equity[symbol])
        tradfi = cfg.get("tradfi_default")
        crypto = cfg.get("crypto_default")
        has_class = bool((cfg.get("asset_class") or {}).get(symbol)) or self._is_rwa_underlying(symbol)
        if has_class and tradfi is not None:
            return floor_leverage(tradfi)
        return floor_leverage(crypto)

    def max_leverage_for(self, underlying: str) -> Optional[float]:
        """Max leverage the UI shows for ``underlying``. Returns None if the
        live config can't be parsed — we never invent a number."""
        cfg = self.get_app_config_cached()
        if not cfg:
            return None
        symbol = self._display_symbol(underlying)
        return self._max_leverage_from_config(symbol, cfg)

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

        cfg = self.get_app_config_cached() or {}
        max_lev = self._max_leverage_from_config(display, cfg)
        fee_bps = cfg.get("taker_fee_bps")
        if fee_bps is None:
            log.warning("Variational: taker fee unavailable for %s — skipping venue", display)
            return None

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
