#!/usr/bin/env python3
"""
exchanges/aster.py - Aster DEX exchange integration.

Covers: orderbook (depth), fees (authenticated /commissionRate endpoint),
and max leverage (from leverageOiRemainingMap API).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode

import requests

from models import StandardizedOrderbook, ExecutionCalculator

log = logging.getLogger(__name__)


class AsterAPI:
    BASE_URL     = "https://fapi.asterdex.com/fapi/v1"
    LEVERAGE_API = "https://www.asterdex.com/bapi/futures/v1/public/future/common/symbol/leverageoi/remaining"
    SYMBOLS_API  = "https://www.asterdex.com/bapi/futures/v1/public/future/simple/symbols"
    # Map short tickers -> Aster symbol names
    SYMBOL_MAP: Dict[str, str] = {
        "XAG":    "XAGUSDT",
        "XAU":    "XAUUSDT",
        "WTI":    "CLUSDT",
        "EURUSD": "EURUSDUSDT",
        "USDJPY": "USDTJPYUSDT",
        "GBPUSD": "GBPUSDUSDT",
        "NVDA":   "NVDAUSDT",
        "TSLA":   "TSLAUSDT",
        "AAPL":   "AAPLUSDT",
        "MSFT":   "MSFTUSDT",
        "AMZN":   "AMZNUSDT",
        "GOOG":   "GOOGUSDT",
        "META":   "METAUSDT",
        "HOOD":   "HOODUSDT",
        "SPY":    "SPYUSDT",
        "QQQ":    "QQQUSDT",
    }

    FEE_FAIL_TTL = 60  # seconds before retrying a failed fee lookup

    def __init__(self):
        self.headers = {'Content-Type': 'application/json'}
        self.leverage_cache: Dict        = {}   # symbol -> max_leverage
        self.leverage_cache_loaded: Dict = {}   # symbol -> bool
        self.fee_cache: Dict             = {}   # symbol -> {taker_fee_bps, maker_fee_bps}
        self._fee_fail_ts: Dict          = {}   # symbol -> timestamp of last failed fee fetch

        self.api_key    = os.getenv("ASTER_API_KEY",    "")
        self.secret_key = os.getenv("ASTER_SECRET_KEY", "")
        self.session    = requests.Session()
        if self.api_key:
            self.session.headers.update({'X-MBX-APIKEY': self.api_key})

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------
    def _sign(self, params: Dict) -> str:
        """Generate HMAC SHA256 signature for request parameters."""
        query_string = urlencode(params)
        return hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    def _signed_request(self, method: str, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """Make a signed API request."""
        params                = params or {}
        params['timestamp']   = int(time.time() * 1000)
        params['recvWindow']  = 10000
        params['signature']   = self._sign(params)
        url = f"{self.BASE_URL}{endpoint}"
        try:
            if method == 'GET':
                response = self.session.get(url, params=params, timeout=30)
            else:
                response = self.session.post(url, data=params, timeout=30)
            if response.status_code == 400:
                try:
                    body = response.json()
                    log.warning("Aster: 400 for %s %s — code=%s msg=%s",
                                method, endpoint, body.get('code'), body.get('msg'))
                except Exception:
                    log.warning("Aster: 400 for %s %s", method, endpoint)
                return None
            response.raise_for_status()
            return response.json()
        except Exception:
            log.exception("Aster API request failed")
            return None

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------
    def get_fees(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Get taker and maker fees for a symbol (authenticated /commissionRate).
        Returns (taker_bps, maker_bps) or (None, None).
        """
        if symbol in self.fee_cache:
            fees = self.fee_cache[symbol]
            return (fees.get('taker_fee_bps'), fees.get('maker_fee_bps'))

        # Short-lived failure cache: NYSE stock symbols on Aster return 400
        # outside trading hours. Don't spam the API, but do retry after TTL
        # so the fee is picked up once the market opens.
        fail_ts = self._fee_fail_ts.get(symbol)
        if fail_ts and (time.time() - fail_ts) < self.FEE_FAIL_TTL:
            return (None, None)

        if not self.api_key or not self.secret_key:
            log.warning("Aster API credentials not configured in .env")
            return (None, None)

        try:
            response = self._signed_request('GET', '/commissionRate', {'symbol': symbol})
            if not response:
                self._fee_fail_ts[symbol] = time.time()
                return (None, None)

            maker_rate = float(response.get('makerCommissionRate', 0))
            taker_rate = float(response.get('takerCommissionRate', 0))

            # Doubles applied: taker fill on Aster matches Hyperliquid/Extended convention
            taker_bps = taker_rate * 10000 * 2
            maker_bps = maker_rate * 10000 * 2

            self.fee_cache[symbol] = {'taker_fee_bps': taker_bps, 'maker_fee_bps': maker_bps}
            return (taker_bps, maker_bps)
        except Exception:
            log.exception("Error fetching Aster fees for %s", symbol)
            return (None, None)

    # ------------------------------------------------------------------
    # Max leverage
    # ------------------------------------------------------------------
    def _fetch_max_leverage(self, symbol: str) -> Optional[int]:
        """
        Fetch max leverage from leverageOiRemainingMap.
        Highest key in the map = max available leverage.
        """
        if self.leverage_cache_loaded.get(symbol):
            return self.leverage_cache.get(symbol)

        try:
            url      = f"{self.LEVERAGE_API}?symbol={symbol}"
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if data.get('success') and data.get('data'):
                    leverage_map = data['data'].get('leverageOiRemainingMap', {})
                    if leverage_map:
                        max_lev = max(int(k) for k in leverage_map.keys())
                        self.leverage_cache[symbol]        = max_lev
                        self.leverage_cache_loaded[symbol] = True
                        return max_lev
        except Exception:
            log.exception("Error fetching Aster max leverage for %s", symbol)

        self.leverage_cache_loaded[symbol] = True
        self.leverage_cache[symbol]        = None
        return None

    def get_max_leverage(self, symbol: str) -> Optional[int]:
        """Get max leverage for a symbol from API."""
        return self._fetch_max_leverage(symbol)

    # ------------------------------------------------------------------
    # Funding fee
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, symbol: str) -> Optional[Dict]:
        url    = f"{self.BASE_URL}/depth"
        params = {'symbol': symbol, 'limit': 1000}
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            if response.status_code != 200:
                return None
            data = response.json()
            if not data.get('bids') or not data.get('asks'):
                return None
            bids = [{'price': float(l[0]), 'qty': float(l[1])} for l in data['bids']]
            asks = [{'price': float(l[0]), 'qty': float(l[1])} for l in data['asks']]
            return {'bids': bids, 'asks': asks}
        except Exception:
            log.exception("Aster get_orderbook error for symbol=%s", symbol)
            return None

    def normalize_orderbook(self, orderbook: Dict) -> Optional[StandardizedOrderbook]:
        """Normalize Aster orderbook to StandardizedOrderbook."""
        if not orderbook:
            return None
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        if not bids or not asks:
            return None

        best_bid = bids[0]['price']
        best_ask = asks[0]['price']
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
            result['fee_bps']       = taker_fee_bps
            result['maker_fee_bps'] = maker_fee_bps
            if symbol:
                result['max_leverage'] = self.get_max_leverage(symbol)

        return result
