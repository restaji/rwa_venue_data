#!/usr/bin/env python3
"""
exchanges/ondo.py - Ondo Perps exchange integration.

Covers: orderbook (depth), fees, and max leverage.

Ondo exposes an Enclave-style REST API. Two public endpoints (authenticated with
an ``x-api-key`` header) give us everything we need:

  Markets:    GET /v1/markets            → per-market taker/maker fee + margin
                                            (positionBracket → maxLeverage) table
  Orderbook:  GET /v1/perps/depth        → live L2 book (?market=<m>&maxOrderbook=100)

Note: the dedicated ``/v1/perps/leverage`` endpoint requires a per-user JWT
session token (not the API key), but the ``marginInfo`` bracket table returned
by ``/v1/markets`` is the same leverage schedule, so we read it from there.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional, Tuple

import requests

from models import StandardizedOrderbook, ExecutionCalculator

log = logging.getLogger(__name__)


class OndoAPI:
    BASE_URL      = "https://api.ondoperps.xyz/v1"
    MAX_ORDERBOOK = 100 # deepest book the depth endpoint returns
    MARKETS_TTL   = 300 # seconds to cache the markets metadata

    def __init__(self):
        self.api_key = os.getenv("ONDO_API_KEY", "")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"x-api-key": self.api_key})

        # market -> {'taker_fee_bps', 'maker_fee_bps', 'max_leverage', 'margin_info'}
        self._markets_cache: Dict[str, Dict] = {}
        self._markets_ts = 0.0

    # ------------------------------------------------------------------
    # Markets metadata (fees + leverage)
    # ------------------------------------------------------------------
    def _load_markets(self, force: bool = False) -> None:
        now = time.time()
        if not force and self._markets_cache and (now - self._markets_ts) < self.MARKETS_TTL:
            return
        try:
            resp = self.session.get(f"{self.BASE_URL}/markets", timeout=30)
            if resp.status_code != 200:
                log.warning("Ondo: /markets returned HTTP %s", resp.status_code)
                return
            data  = resp.json()
            pairs = (data.get("result", {}).get("perps", {}) or {}).get("tradingPairs") or []
            cache: Dict[str, Dict] = {}
            for m in pairs:
                market = m.get("market")
                if not market:
                    continue
                try:
                    taker_bps = float(m.get("takerFee", 0)) * 10000
                    maker_bps = float(m.get("makerFee", 0)) * 10000
                except (TypeError, ValueError):
                    taker_bps = maker_bps = None

                margin_info = m.get("marginInfo") or []
                max_lev = None
                for bracket in margin_info:
                    try:
                        lev = float(bracket.get("maxLeverage"))
                    except (TypeError, ValueError):
                        continue
                    if max_lev is None or lev > max_lev:
                        max_lev = lev

                cache[market] = {
                    "taker_fee_bps": taker_bps,
                    "maker_fee_bps": maker_bps,
                    "max_leverage":  max_lev,
                    "margin_info":   margin_info,
                }
            if cache:
                self._markets_cache = cache
                self._markets_ts    = now
        except Exception:
            log.exception("Ondo: error loading /markets")

    def _market_meta(self, market: str) -> Optional[Dict]:
        self._load_markets()
        return self._markets_cache.get(market)

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------
    def get_fees(self, market: str) -> Tuple[Optional[float], Optional[float]]:
        """Return (taker_bps, maker_bps) for a market, or (None, None)."""
        meta = self._market_meta(market)
        if not meta:
            return (None, None)
        return (meta.get("taker_fee_bps"), meta.get("maker_fee_bps"))

    # ------------------------------------------------------------------
    # Max leverage
    # ------------------------------------------------------------------
    def get_max_leverage(self, market: str) -> Optional[float]:
        """Max leverage from the markets marginInfo bracket table."""
        meta = self._market_meta(market)
        return meta.get("max_leverage") if meta else None

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, market: str) -> Optional[Dict]:
        params = {"market": market, "maxOrderbook": self.MAX_ORDERBOOK}
        try:
            resp = self.session.get(f"{self.BASE_URL}/perps/depth", params=params, timeout=30)
            if resp.status_code != 200:
                return None
            result = resp.json().get("result", {}) or {}
            raw_bids = result.get("bids") or []
            raw_asks = result.get("asks") or []
            if not raw_bids or not raw_asks:
                return None
            bids = [{"price": float(l[0]), "qty": float(l[1])} for l in raw_bids]
            asks = [{"price": float(l[0]), "qty": float(l[1])} for l in raw_asks]
            return {"bids": bids, "asks": asks}
        except Exception:
            log.exception("Ondo get_orderbook error for market=%s", market)
            return None

    def normalize_orderbook(self, orderbook: Dict) -> Optional[StandardizedOrderbook]:
        """Normalize Ondo orderbook to StandardizedOrderbook."""
        if not orderbook:
            return None
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return None

        best_bid = bids[0]["price"]
        best_ask = asks[0]["price"]
        if best_bid <= 0 or best_ask <= 0:
            return None

        mid_price = (best_bid + best_ask) / 2

        return StandardizedOrderbook(
            bids=bids, asks=asks,
            best_bid=best_bid, best_ask=best_ask,
            mid_price=mid_price, timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(
        self, orderbook: Dict, order_size_usd: float, symbol: str = None
    ) -> Optional[Dict]:
        """Calculate execution cost using ExecutionCalculator."""
        std_orderbook = self.normalize_orderbook(orderbook)
        if not std_orderbook:
            return None

        taker_fee_bps, maker_fee_bps = self.get_fees(symbol) if symbol else (None, None)
        if taker_fee_bps is None:
            return None

        result = ExecutionCalculator.calculate_execution_cost(
            std_orderbook, order_size_usd,
            open_fee_bps=taker_fee_bps,
            close_fee_bps=0.0,
        )

        if result:
            result["fee_bps"]       = taker_fee_bps
            result["maker_fee_bps"] = maker_fee_bps
            if symbol:
                result["max_leverage"] = self.get_max_leverage(symbol)

        return result
