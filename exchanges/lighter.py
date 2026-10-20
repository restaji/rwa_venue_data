#!/usr/bin/env python3
"""
exchanges/lighter.py - Lighter exchange integration.

Covers: orderbook, fees (from orderBookDetails API), and max leverage
(derived from min_initial_margin_fraction).
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

from models import StandardizedOrderbook, ExecutionCalculator, floor_leverage


class LighterAPI:
    def __init__(self):
        self.base_url           = "https://mainnet.zklighter.elliot.ai/api/v1"
        self.headers            = {'Content-Type': 'application/json'}
        self.market_cache: Dict = {}   # market_id -> {taker_fee_bps, maker_fee_bps, min_initial_margin_fraction}
        self.market_cache_loaded = False

    # ------------------------------------------------------------------
    # Fees & max leverage
    # ------------------------------------------------------------------
    def _cache_one_market(self, market: Dict) -> None:
        market_id = market.get('market_id')
        if market_id is None:
            return
        key = int(market_id) if isinstance(market_id, (int, float)) else market_id
        min_margin = market.get('min_initial_margin_fraction')
        self.market_cache[key] = {
            'taker_fee_bps':              float(market.get('taker_fee', '0')) * 100,
            'maker_fee_bps':              float(market.get('maker_fee', '0')) * 100,
            'min_initial_margin_fraction': float(min_margin) if min_margin else None,
            'symbol':                     market.get('symbol') or market.get('market_symbol') or market.get('name'),
            'status':                     market.get('status'),
        }

    def _load_market_cache(self):
        """Load fees and margin info from orderBookDetails API for all perp markets."""
        if self.market_cache_loaded:
            return
        try:
            url      = f"{self.base_url}/orderBookDetails"
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code != 200:
                return
            for market in response.json().get('order_book_details', []):
                self._cache_one_market(market)
            self.market_cache_loaded = True
        except (TypeError, ValueError, KeyError, OSError):
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
        max_leverage = 10000 / min_initial_margin_fraction, floored to a whole number.
        """
        self._load_market_cache()
        key = self._market_key(market_id)
        market_data = self.market_cache.get(key, {}) if key is not None else {}
        min_margin  = market_data.get('min_initial_margin_fraction')
        if min_margin and min_margin > 0:
            return floor_leverage(10000 / min_margin)
        return None

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, market_id: int) -> Optional[Dict]:
        url = f"{self.base_url}/orderBookOrders?market_id={market_id}&limit=250"
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except (TypeError, ValueError, KeyError, OSError):
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

        return result
