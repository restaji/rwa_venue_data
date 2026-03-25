#!/usr/bin/env python3
"""
exchanges/avantis.py - Avantis exchange integration.

Covers: dynamic fee (skewEqParams opening fee), dynamic spread (risk API),
closing fee, and max leverage. Oracle-based – no traditional orderbook.
"""
from __future__ import annotations

import logging
import math
import time
from decimal import Decimal
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)


class AvantisAPI:
    """Dynamic fee and spread calculation for Avantis."""

    SOCKET_API   = "https://socket-api-pub.avantisfi.com/socket-api/v1/data"
    RISK_API     = "https://risk-api.avantisfi.com/spread/dynamic"
    DUMMY_TRADER = "0x1234567890123456789012345678901234567890"

    PAIRS = {
        "XAU": 21, "XAG": 20, "WTI": 65,  
        "EURUSD": 11, "GBPUSD": 13, "USDJPY": 12,
        "SPY": 78, "QQQ": 79,
        "NVDA": 81, "TSLA": 86, "AAPL": 82, "MSFT": 84,
        "AMZN": 83, "GOOG": 87, "META": 85, "COIN": 80, "HOOD": 91,
    }

    def __init__(self):
        self._pair_data  = None
        self._group_info = None
        self._last_fetch = 0
        self._cache_ttl  = 30

    def _fetch_socket_data(self):
        now = time.time()
        if self._pair_data and (now - self._last_fetch) < self._cache_ttl:
            return
        try:
            resp = requests.get(self.SOCKET_API, timeout=30)
            data = resp.json().get("data", {})
            self._pair_data  = data.get("pairInfos", {})
            self._group_info = data.get("groupInfo", {})
            self._last_fetch = now
        except Exception:
            log.exception("Avantis API error")
            self._pair_data  = {}
            self._group_info = {}

    def _get_pair_info(self, asset_key: str) -> Optional[Tuple[str, Dict]]:
        self._fetch_socket_data()
        pair_idx = self.PAIRS.get(asset_key.upper())
        if pair_idx is None:
            return None
        pair_info = self._pair_data.get(str(pair_idx))
        return (str(pair_idx), pair_info) if pair_info else None

    def _calculate_opening_fee(self, pair_info: Dict, position_size: float, is_long: bool = True) -> float:
        long_oi     = pair_info.get("openInterest", {}).get("long",  0)
        short_oi    = pair_info.get("openInterest", {}).get("short", 0)
        skew_params = pair_info.get("skewEqParams", [[0, 450]])

        if is_long:
            divisor           = long_oi + position_size + short_oi
            open_interest_pct = math.floor((100 * short_oi) / (divisor if divisor else 1))
        else:
            divisor           = short_oi + position_size + long_oi
            open_interest_pct = math.floor((100 * long_oi)  / (divisor if divisor else 1))

        pct_index = min(int(Decimal(str(open_interest_pct)) / Decimal('10')), len(skew_params) - 1)
        param1, param2 = skew_params[pct_index][0], skew_params[pct_index][1]
        return ((param1 * open_interest_pct + param2) / 10000) * 100

    def _fetch_dynamic_spread(self, pair_index: int, position_size: float, is_long: bool, is_pnl: bool) -> Optional[float]:
        params = {
            "pairIndex":        pair_index,
            "positionSizeUsdc": int(position_size * (10 ** 18)),
            "isLong":           str(is_long).lower(),
            "isPnl":            str(is_pnl).lower(),
            "trader":           self.DUMMY_TRADER,
        }
        try:
            data = requests.get(self.RISK_API, params=params, timeout=30).json()
            return float(data.get("spreadP", 0)) / (10 ** 10) * 100
        except Exception:
            return None

    def _get_spread(self, pair_idx: str, pair_info: Dict, position_size: float, is_long: bool = True) -> float:
        group_index = pair_info.get("groupIndex", 0)
        group       = self._group_info.get(str(group_index), {})
        if group.get("isSpreadDynamic", False):
            spread = self._fetch_dynamic_spread(int(pair_idx), position_size, is_long, is_pnl=False)
            if spread is not None:
                return spread
        return pair_info.get("spreadP", 0) * 100

    def calculate_cost(self, asset_key: str, order_size_usd: float, is_long: bool = True) -> Optional[Dict]:
        result = self._get_pair_info(asset_key)
        if not result:
            return None
        pair_idx, pair_info = result

        max_wallet_oi = pair_info.get("maxWalletOI", float('inf'))
        filled        = order_size_usd <= max_wallet_oi
        filled_usd    = min(order_size_usd, max_wallet_oi)
        unfilled_usd  = max(0, order_size_usd - max_wallet_oi)
        position_size = filled_usd

        open_fee_bps  = self._calculate_opening_fee(pair_info, position_size, is_long=is_long)
        close_fee_bps = pair_info.get("closeFeeP", 0) * 100
        spread_bps    = self._get_spread(pair_idx, pair_info, position_size, is_long=is_long)
        total_cost    = spread_bps + open_fee_bps + close_fee_bps

        margin_fee            = pair_info.get("marginFee", {})
        holding_1h_long_pct   = margin_fee.get("long",  0)
        holding_1h_short_pct  = margin_fee.get("short", 0)
        holding_24h_long_pct  = math.floor(holding_1h_long_pct  * 24 * 1_000_000) / 1_000_000
        holding_24h_short_pct = math.floor(holding_1h_short_pct * 24 * 1_000_000) / 1_000_000

        leverages      = pair_info.get("leverages", {})
        storage_params = pair_info.get("storagePairParams", {})
        max_lev = leverages.get("maxLeverage")

        return {
            'symbol':               asset_key,
            'max_leverage':         max_lev,
            'executed':             True if filled else 'PARTIAL',
            'mid_price':            0,
            'slippage_bps':         spread_bps,
            'opening_slippage_bps': spread_bps,
            'closing_slippage_bps': 0.0,
            'buy_slippage_bps':     spread_bps,
            'sell_slippage_bps':    0.0,
            'slippage_type':        'opening_closing',
            'open_fee_bps':         open_fee_bps,
            'close_fee_bps':        close_fee_bps,
            'maker_fee_bps':        open_fee_bps,
            'total_cost_bps':       total_cost,
            'filled':               filled,
            'order_size_usd':       order_size_usd,
            'filled_usd':           filled_usd,
            'unfilled_usd':         unfilled_usd,
            'max_wallet_oi':        max_wallet_oi,
            'buy':  {'filled': filled, 'filled_usd': filled_usd, 'unfilled_usd': unfilled_usd, 'levels_used': 1, 'slippage_bps': spread_bps},
            'sell': {'filled': filled, 'filled_usd': filled_usd, 'unfilled_usd': unfilled_usd, 'levels_used': 1, 'slippage_bps': 0.0},
            'holding_fee_1h_long_pct':   round(holding_1h_long_pct,   6),
            'holding_fee_1h_short_pct':  round(holding_1h_short_pct,  6),
            'holding_fee_24h_long_pct':  holding_24h_long_pct,
            'holding_fee_24h_short_pct': holding_24h_short_pct,
            'timestamp':                 time.time(),
        }
