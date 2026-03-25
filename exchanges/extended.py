#!/usr/bin/env python3
"""
exchanges/extended.py - Extended Exchange (Starknet) integration.

Covers: orderbook, fees (from /user/fees API), and max leverage
(from /info/markets API).
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

from models import StandardizedOrderbook, ExecutionCalculator


class ExtendedAPI:
    """Client for Extended Exchange (Starknet) orderbook data."""

    BASE_URL       = "https://api.starknet.extended.exchange/api/v1"

    def __init__(self):
        self.API_KEY = os.getenv("EXTENDED_API_KEY", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "X-API-Key":    self.API_KEY,
        })
        self.market_cache: Dict        = {}   # market -> {max_leverage}
        self.market_cache_loaded: Dict = {}
        self.fee_cache: Dict           = {}   # market -> {taker_fee_bps, maker_fee_bps}

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------
    def get_fees(self, market: str) -> Tuple[Optional[float], Optional[float]]:
        """Get taker and maker fees from /api/v1/user/fees?market={market}."""
        if market in self.fee_cache:
            c = self.fee_cache[market]
            return (c.get('taker_fee_bps'), c.get('maker_fee_bps'))
        try:
            url      = f"{self.BASE_URL}/user/fees?market={market}"
            response = self.session.get(url, timeout=30)
            if response.status_code != 200:
                return (None, None)
            data     = response.json()
            raw_data = data.get('data', data)

            if isinstance(raw_data, list) and raw_data:
                raw = raw_data[0]
            elif isinstance(raw_data, dict):
                raw = raw_data
            else:
                return (None, None)

            taker = raw.get('takerFeeRate', raw.get('takerFee', raw.get('taker_fee')))
            maker = raw.get('makerFeeRate', raw.get('makerFee', raw.get('maker_fee')))

            if taker is None or maker is None:
                return (None, None)

            taker_bps = float(taker) * 10000
            maker_bps = float(maker) * 10000

            self.fee_cache[market] = {'taker_fee_bps': taker_bps, 'maker_fee_bps': maker_bps}
            return (taker_bps, maker_bps)
        except Exception:
            log.exception("Error fetching Extended fees for %s", market)
            return (None, None)

    # ------------------------------------------------------------------
    # Max leverage
    # ------------------------------------------------------------------
    def _load_market_info(self, market: str):
        """Fetch and cache market info including max leverage."""
        if self.market_cache_loaded.get(market):
            return
        try:
            url      = f"{self.BASE_URL}/info/markets?market={market}"
            response = self.session.get(url, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'OK':
                    markets = data.get('data', [])
                    if markets:
                        trading_config = markets[0].get('tradingConfig', {})
                        self.market_cache[market]        = {'max_leverage': float(trading_config.get('maxLeverage', 0))}
                        self.market_cache_loaded[market] = True
        except Exception:
            log.exception("Error fetching Extended market info for %s", market)

    def get_max_leverage(self, market: str) -> Optional[float]:
        """Get max leverage for a market."""
        self._load_market_info(market)
        return self.market_cache.get(market, {}).get('max_leverage')

    # ------------------------------------------------------------------
    # Funding fee
    # ------------------------------------------------------------------
    def get_holding_fee(self, market: str) -> Dict:
        """
        Fetch current holding fee for *market* from the public /stats endpoint.
        `fundingRate` is the 1H rate as a plain fraction (e.g. -0.000007).
        Positive = longs pay shorts; negative = shorts pay longs.
        """
        try:
            url  = f"{self.BASE_URL}/info/markets/{market}/stats"
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'OK':
                    rate_1h_raw     = float(data.get('data', {}).get('fundingRate', 0) or 0)
                    holding_1h_pct  = rate_1h_raw * 100
                    holding_24h_pct = math.floor(holding_1h_pct * 24 * 1_000_000) / 1_000_000
                    return {
                        'holding_fee_1h_pct':  round(holding_1h_pct, 6),
                        'holding_fee_24h_pct': holding_24h_pct,
                    }
        except Exception:
            log.exception("Extended holding fee error for %s", market)
        return {'holding_fee_1h_pct': 0.0, 'holding_fee_24h_pct': 0.0}

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, market: str) -> Optional[Dict]:
        try:
            url      = f"{self.BASE_URL}/info/markets/{market}/orderbook"
            response = self.session.get(url, timeout=30)
            if response.status_code != 200:
                return None
            data = response.json()
            return data.get('data') if data.get('status') == 'OK' else None
        except Exception:
            log.exception("Extended API error for %s", market)
            return None

    def normalize_orderbook(self, orderbook: Dict) -> Optional[StandardizedOrderbook]:
        if not orderbook:
            return None
        bids = orderbook.get('bid', [])
        asks = orderbook.get('ask', [])
        if not bids or not asks:
            return None

        best_bid = float(bids[0]['price'])
        best_ask = float(asks[0]['price'])
        if best_bid <= 0 or best_ask <= 0:
            return None

        mid_price = (best_bid + best_ask) / 2
        std_bids  = [{'price': float(b['price']), 'qty': float(b['qty'])} for b in bids]
        std_asks  = [{'price': float(a['price']), 'qty': float(a['qty'])} for a in asks]

        return StandardizedOrderbook(
            bids=std_bids, asks=std_asks,
            best_bid=best_bid, best_ask=best_ask,
            mid_price=mid_price, timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(self, orderbook: Dict, order_size_usd: float, market: str = None) -> Optional[Dict]:
        """Calculate execution cost using ExecutionCalculator."""
        std_orderbook = self.normalize_orderbook(orderbook)
        if not std_orderbook or not market:
            return None

        taker_bps, maker_bps = self.get_fees(market)
        if taker_bps is None or maker_bps is None:
            return None

        result = ExecutionCalculator.calculate_execution_cost(
            std_orderbook, order_size_usd,
            open_fee_bps=taker_bps,
            close_fee_bps=taker_bps,
        )

        if result:
            result['fee_bps']       = taker_bps
            result['maker_fee_bps'] = maker_bps
            result['max_leverage']  = self.get_max_leverage(market)
            holding = self.get_holding_fee(market)
            result.update(holding)

        return result
