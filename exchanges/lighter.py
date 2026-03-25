#!/usr/bin/env python3
"""
exchanges/lighter.py - Lighter exchange integration.

Covers: orderbook, fees (from orderBookDetails API), and max leverage
(derived from min_initial_margin_fraction).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

from models import StandardizedOrderbook, ExecutionCalculator


class LighterAPI:
    def __init__(self):
        self.base_url           = "https://mainnet.zklighter.elliot.ai/api/v1"
        self.headers            = {'Content-Type': 'application/json'}
        self.market_cache: Dict = {}   # market_id -> {taker_fee_bps, maker_fee_bps, min_initial_margin_fraction}
        self.market_cache_loaded = False
        self.funding_cache: Dict = {}  # market_id -> funding_rate (fraction, 8H period on Lighter)
        self._last_funding_fetch = 0
        self._funding_cache_ttl  = 60  # seconds

    # ------------------------------------------------------------------
    # Fees & max leverage
    # ------------------------------------------------------------------
    def _load_market_cache(self):
        """Load fees and margin info from orderBookDetails API for all perp markets."""
        if self.market_cache_loaded:
            return

        try:
            url      = f"{self.base_url}/orderBookDetails"
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code == 200:
                data    = response.json()
                markets = data.get('order_book_details', [])
                for m in markets:
                    market_id = m.get('market_id')
                    if market_id is not None:
                        key = int(market_id) if isinstance(market_id, (int, float)) else market_id
                        taker       = float(m.get('taker_fee', '0')) * 100   # → bps
                        maker       = float(m.get('maker_fee', '0')) * 100
                        min_margin  = m.get('min_initial_margin_fraction')
                        self.market_cache[key] = {
                            'taker_fee_bps':             taker,
                            'maker_fee_bps':             maker,
                            'min_initial_margin_fraction': float(min_margin) if min_margin else None,
                        }
                self.market_cache_loaded = True
        except Exception:
            log.exception("Error loading Lighter market cache")

    def _market_key(self, market_id: Optional[int]) -> Optional[int]:
        """Normalize market_id to int for cache lookup."""
        if market_id is None:
            return None
        return int(market_id) if isinstance(market_id, (int, float)) else market_id

    def get_fees(self, market_id: int) -> Tuple[Optional[float], Optional[float]]:
        """Get taker and maker fees for a market_id."""
        self._load_market_cache()
        key = self._market_key(market_id)
        market_data = self.market_cache.get(key) if key is not None else None
        if not market_data:
            return (None, None)
        return (market_data.get('taker_fee_bps'), market_data.get('maker_fee_bps'))

    def get_max_leverage(self, market_id: int) -> Optional[float]:
        """
        Get max leverage calculated from min_initial_margin_fraction.
        max_leverage = 10000 / min_initial_margin_fraction. Rounded to 2 decimals for display.
        """
        self._load_market_cache()
        key = self._market_key(market_id)
        market_data = self.market_cache.get(key, {}) if key is not None else {}
        min_margin  = market_data.get('min_initial_margin_fraction')
        if min_margin and min_margin > 0:
            lev = 10000 / min_margin
            return round(lev, 2)
        return None

    # ------------------------------------------------------------------
    # Funding fee
    # ------------------------------------------------------------------
    def _fetch_funding_rates(self):
        """Fetch current funding rates from /funding-rates (reference rates per market)."""
        now = time.time()
        if self.funding_cache and (now - self._last_funding_fetch) < self._funding_cache_ttl:
            return
        try:
            resp = requests.get(f"{self.base_url}/funding-rates", headers=self.headers, timeout=30)
            if resp.status_code == 200:
                for entry in resp.json().get('funding_rates', []):
                    if entry.get('exchange') != 'lighter':
                        continue
                    mid = entry.get('market_id')
                    rate = entry.get('rate', 0)
                    if mid is not None:
                        key = int(mid) if isinstance(mid, (int, float)) else mid
                        self.funding_cache[key] = float(rate)
                self._last_funding_fetch = now
        except Exception:
            log.exception("Error fetching Lighter funding rates")

    def get_holding_fee(self, market_id: int) -> Dict:
        """
        Return 1H and 24H holding fee for *market_id* as % of notional.
        The /funding-rates API returns an 8H rate as a fraction (e.g. 0.0001 = 0.01%).
        Positive = longs pay shorts; negative = shorts pay longs.
        """
        self._fetch_funding_rates()
        key = self._market_key(market_id)
        rate_8h_raw     = self.funding_cache.get(key, 0.0) if key is not None else 0.0
        holding_8h_pct  = rate_8h_raw * 100
        holding_1h_pct  = holding_8h_pct / 8
        holding_24h_pct = math.floor(holding_8h_pct * 3 * 1_000_000) / 1_000_000
        return {
            'holding_fee_1h_pct':  round(holding_1h_pct, 6),
            'holding_fee_24h_pct': holding_24h_pct,
        }

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, market_id: int) -> Optional[Dict]:
        url = f"{self.base_url}/orderBookOrders?market_id={market_id}&limit=250"
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception:
            log.exception("Lighter get_orderbook error for market_id=%s", market_id)
            return None

    def normalize_orderbook(self, orderbook: Dict) -> Optional[StandardizedOrderbook]:
        """Normalize Lighter orderbook to StandardizedOrderbook."""
        if not orderbook:
            return None

        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if not bids or not asks:
            return None

        best_bid = float(bids[0].get('price', 0))
        best_ask = float(asks[0].get('price', 0))
        if best_bid <= 0 or best_ask <= 0:
            return None

        mid_price = (best_bid + best_ask) / 2

        std_bids = [{'price': float(b.get('price', 0)), 'qty': float(b.get('remaining_base_amount', 0))} for b in bids]
        std_asks = [{'price': float(a.get('price', 0)), 'qty': float(a.get('remaining_base_amount', 0))} for a in asks]

        return StandardizedOrderbook(
            bids=std_bids, asks=std_asks,
            best_bid=best_bid, best_ask=best_ask,
            mid_price=mid_price, timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(
        self, orderbook: Dict, order_size_usd: float, market_id: int = None
    ) -> Optional[Dict]:
        """Calculate execution cost using ExecutionCalculator."""
        std_orderbook = self.normalize_orderbook(orderbook)
        if not std_orderbook or not market_id:
            return None

        taker_fee_bps, maker_fee_bps = self.get_fees(market_id)
        if taker_fee_bps is None:
            return None

        result = ExecutionCalculator.calculate_execution_cost(
            std_orderbook, order_size_usd,
            open_fee_bps=taker_fee_bps,
            close_fee_bps=taker_fee_bps,
        )

        if result:
            result['fee_bps']        = taker_fee_bps
            result['maker_fee_bps']  = maker_fee_bps
            result['max_leverage']   = self.get_max_leverage(market_id)
            holding                  = self.get_holding_fee(market_id)
            result.update(holding)

        return result
