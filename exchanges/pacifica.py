#!/usr/bin/env python3
"""
exchanges/pacifica.py - Pacifica exchange integration.

Covers: orderbook (native-tick L2, agg_level=1), base taker/maker fees, and
max leverage – all from public REST APIs (no auth required).

Public APIs (https://api.pacifica.fi/api/v1):
  Markets:    GET /info           → per-symbol max_leverage + contract specs
  Fees:       GET /info/fees      → maker/taker tiers (level 0 = base retail)
  Orderbook:  GET /book?symbol=X  → 10 levels of bids/asks (p/a/n)
  Prices:     GET /info/prices    → mid / mark / hourly funding

The UI route https://app.pacifica.fi/trade/GOLD is a display alias; the live
API symbol is XAU. GOLD is remapped in SYMBOL_ALIASES.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

from models import StandardizedOrderbook, ExecutionCalculator, floor_leverage

log = logging.getLogger(__name__)


class PacificaAPI:
    BASE_URL = "https://api.pacifica.fi/api/v1"
    INFO_TTL = 300   # seconds
    FEES_TTL = 300

    # Pacifica's finest grouping, and the UI/API default. Coarser values
    # (10/100/1000/10000) only bucket ticks for display — they do not add
    # real levels. REST still returns at most 10 price levels.
    AGG_LEVEL = 1

    # UI tickers that differ from the API symbol.
    SYMBOL_ALIASES: Dict[str, str] = {
        "GOLD": "XAU",
        "SILVER": "XAG",
        "WTI": "CL",
        "GOOG": "GOOGL",
        "US500": "SP500",
        "SPY": "SP500",
        "SKHY": "SKHYNIX",
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept":     "application/json",
            "User-Agent": "splitting-comparator/1.0",
        })
        # symbol -> {max_leverage, tick_size, lot_size, ...}
        self._meta_cache: Dict[str, Dict] = {}
        self._meta_ts = 0.0
        # (taker_bps, maker_bps) from fee tier 0
        self._fees: Tuple[Optional[float], Optional[float]] = (None, None)
        self._fees_ts = 0.0

    def resolve_symbol(self, symbol: str) -> str:
        """Map a comparator/UI ticker to Pacifica's API symbol."""
        return self.SYMBOL_ALIASES.get(symbol.upper(), symbol.upper())

    # ------------------------------------------------------------------
    # Market metadata (max leverage)
    # ------------------------------------------------------------------
    def _load_meta(self, force: bool = False) -> None:
        now = time.time()
        if not force and self._meta_cache and (now - self._meta_ts) < self.INFO_TTL:
            return
        try:
            resp = self.session.get(f"{self.BASE_URL}/info", timeout=30)
            if resp.status_code != 200:
                log.warning("Pacifica: /info returned HTTP %s", resp.status_code)
                return
            payload = resp.json()
            if not payload.get("success"):
                log.warning("Pacifica: /info success=false: %s", payload.get("error"))
                return
            cache: Dict[str, Dict] = {}
            for m in payload.get("data") or []:
                sym = m.get("symbol")
                if not sym:
                    continue
                try:
                    max_lev = floor_leverage(m.get("max_leverage"))
                except (TypeError, ValueError):
                    max_lev = None
                cache[str(sym).upper()] = {
                    "max_leverage": max_lev,
                    "tick_size":    m.get("tick_size"),
                    "lot_size":     m.get("lot_size"),
                    "base_asset":   m.get("base_asset"),
                }
            if cache:
                self._meta_cache = cache
                self._meta_ts = now
        except Exception:
            log.exception("Pacifica: error loading /info")

    def _meta(self, symbol: str) -> Optional[Dict]:
        self._load_meta()
        return self._meta_cache.get(self.resolve_symbol(symbol))

    # ------------------------------------------------------------------
    # Fees  (venue-wide; level 0 = $0 30-day volume)
    # ------------------------------------------------------------------
    def _load_fees(self, force: bool = False) -> None:
        now = time.time()
        if not force and self._fees[0] is not None and (now - self._fees_ts) < self.FEES_TTL:
            return
        try:
            resp = self.session.get(f"{self.BASE_URL}/info/fees", timeout=30)
            if resp.status_code != 200:
                log.warning("Pacifica: /info/fees returned HTTP %s", resp.status_code)
                return
            payload = resp.json()
            if not payload.get("success"):
                log.warning("Pacifica: /info/fees success=false: %s", payload.get("error"))
                return
            tiers: List[Dict] = payload.get("data") or []
            base = None
            for t in tiers:
                if t.get("level") == 0:
                    base = t
                    break
            if base is None and tiers:
                base = min(tiers, key=lambda t: t.get("level", 99))
            if not base:
                return
            taker_bps = round(float(base["taker_fee_rate"]) * 10_000, 4)
            maker_bps = round(float(base["maker_fee_rate"]) * 10_000, 4)
            self._fees = (taker_bps, maker_bps)
            self._fees_ts = now
        except Exception:
            log.exception("Pacifica: error loading /info/fees")

    def get_fees(self, symbol: Optional[str] = None) -> Tuple[Optional[float], Optional[float]]:
        """Return (taker_bps, maker_bps). Fees are venue-wide; ``symbol`` is unused."""
        self._load_fees()
        return self._fees

    def get_max_leverage(self, symbol: str) -> Optional[int]:
        meta = self._meta(symbol)
        return floor_leverage(meta.get("max_leverage") if meta else None)

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, symbol: str, agg_level: int = AGG_LEVEL) -> Optional[Dict]:
        """Fetch raw book. ``l[0]`` = bids (best first), ``l[1]`` = asks (best first)."""
        api_symbol = self.resolve_symbol(symbol)
        params = {"symbol": api_symbol, "agg_level": agg_level}
        try:
            resp = self.session.get(f"{self.BASE_URL}/book", params=params, timeout=30)
            if resp.status_code != 200:
                log.warning("Pacifica book %s agg=%s HTTP %s", api_symbol, agg_level, resp.status_code)
                return None
            payload = resp.json()
            if not payload.get("success"):
                return None
            return payload.get("data")
        except Exception:
            log.exception("Pacifica get_orderbook error for %s", api_symbol)
            return None

    def normalize_orderbook(self, orderbook: Dict) -> Optional[StandardizedOrderbook]:
        if not orderbook:
            return None
        levels = orderbook.get("l") or []
        raw_bids = levels[0] if len(levels) > 0 else []
        raw_asks = levels[1] if len(levels) > 1 else []
        if not raw_bids or not raw_asks:
            return None

        def parse(entries):
            out = []
            for e in entries:
                try:
                    price = float(e.get("p", 0))
                    qty   = float(e.get("a", 0))  # base-asset size
                except (TypeError, ValueError, AttributeError):
                    continue
                if price > 0 and qty > 0:
                    out.append({"price": price, "qty": qty})
            return out

        bids = parse(raw_bids)
        asks = parse(raw_asks)
        if not bids or not asks:
            return None

        best_bid = bids[0]["price"]
        best_ask = asks[0]["price"]
        if best_bid <= 0 or best_ask <= 0:
            return None

        ts_ms = orderbook.get("t")
        try:
            timestamp = float(ts_ms) / 1000.0 if ts_ms else time.time()
        except (TypeError, ValueError):
            timestamp = time.time()

        return StandardizedOrderbook(
            bids=bids, asks=asks,
            best_bid=best_bid, best_ask=best_ask,
            mid_price=(best_bid + best_ask) / 2,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(
        self,
        orderbook: Dict,
        order_size_usd: float,
        symbol: Optional[str] = None,
        anchor_mid_price: Optional[float] = None,
    ) -> Optional[Dict]:
        std = self.normalize_orderbook(orderbook)
        if not std:
            return None

        if anchor_mid_price:
            std = StandardizedOrderbook(
                bids=std.bids, asks=std.asks,
                best_bid=std.best_bid, best_ask=std.best_ask,
                mid_price=anchor_mid_price, timestamp=std.timestamp,
            )

        taker_fee_bps, maker_fee_bps = self.get_fees(symbol)
        if taker_fee_bps is None:
            return None

        result = ExecutionCalculator.calculate_execution_cost(
            std, order_size_usd,
            open_fee_bps=taker_fee_bps,
            close_fee_bps=taker_fee_bps,
        )
        if not result:
            return None

        result["fee_bps"]       = taker_fee_bps
        result["maker_fee_bps"] = maker_fee_bps
        if symbol:
            result["symbol"]       = self.resolve_symbol(symbol)
            result["max_leverage"] = self.get_max_leverage(symbol)
        return result

    def get_optimal_execution(self, symbol: str, order_size_usd: float) -> Optional[Dict]:
        """Walk the native-tick book (agg_level=1). Unfilled size stays PARTIAL."""
        taker_fee_bps, maker_fee_bps = self.get_fees(symbol)
        if taker_fee_bps is None:
            return None

        api_symbol = self.resolve_symbol(symbol)
        raw = self.get_orderbook(api_symbol, agg_level=self.AGG_LEVEL)
        result = self.calculate_execution_cost(raw, order_size_usd, symbol=api_symbol)
        if not result:
            return None

        result["symbol"]        = api_symbol
        result["max_leverage"]  = self.get_max_leverage(api_symbol)
        result["fee_bps"]       = taker_fee_bps
        result["maker_fee_bps"] = maker_fee_bps
        result["agg_level"]     = self.AGG_LEVEL
        result["true_mid"]      = result.get("mid_price")
        return result
