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

    SOCKET_API   = "https://socket-api-pub.avantisfi.com/socket-api/v1/data?override=true"
    RISK_API     = "https://risk-api.avantisfi.com/v2/dynamic-spread"  # SpreadController_getSpreadV2
    DUMMY_TRADER = "0x1234567890123456789012345678901234567890"

    # Partial-fill search: when a full-size quote is unavailable, binary-search
    # for the largest quotable size. Caps API round-trips and stops once the
    # bracket is within tolerance (fraction of requested size).
    _MAX_SPREAD_SEARCH_ITERS = 8
    _SPREAD_SEARCH_TOL       = 0.02

    PAIRS = {
        "XAU": 21, "XAG": 20, "WTI": 96, "BRENT": 97,  
        "EURUSD": 11, "GBPUSD": 13, "USDJPY": 12,
        "SPY": 78, "QQQ": 79,
        "NVDA": 81, "TSLA": 86, "AAPL": 82, "MSFT": 84,
        "AMZN": 83, "GOOG": 87, "META": 85, "COIN": 80, "HOOD": 91,
        "SPCX": 92
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

    def _fetch_dynamic_spread(self, pair_index: int, position_size: float, is_long: bool, is_pnl: bool, max_retries: int = 5) -> Optional[Dict]:
        """
        Fetch dynamic spread from Risk API v2 with retries.
        Endpoint: GET /v2/dynamic-spread/{pairIndex}
        Returns dict with spread_bps and v2 metadata breakdown.
        """
        url = f"{self.RISK_API}/{pair_index}"
        params = {
            "collateralUsdc": int(position_size),
            "isLong":         str(is_long).lower(),
            "leverage":       1,
            "isPnl":          str(is_pnl).lower(),
            "precision":      18,
            "trader":         self.DUMMY_TRADER,
        }
        scale = 10 ** 18

        def to_bps(raw):
            try:
                return float(raw) / scale * 100
            except (TypeError, ValueError):
                return 0.0

        last_result = None
        for attempt in range(max_retries):
            try:
                data       = requests.get(url, params=params, timeout=30).json()
                metadata   = data.get("metadata", {})
                raw_spread = data.get("dynamicSpreadPct")
                error_code = metadata.get("errorCode")
                last_result = {
                    "spread_bps":               to_bps(raw_spread) if raw_spread is not None else None,
                    "fixed_spread_bps":         to_bps(metadata.get("fixedSpreadPct", 0)),
                    "price_impact_spread_bps":  to_bps(metadata.get("priceImpactSpreadPct", 0)),
                    "skew_impact_spread_bps":   to_bps(metadata.get("skewImpactSpreadPct", 0)),
                    "is_dynamic_spread_enabled": metadata.get("isDynamicSpreadEnabled", False),
                    "error_code":               error_code,
                    "lp_bids":                  metadata.get("bids"),
                    "lp_asks":                  metadata.get("asks"),
                }
                if last_result["spread_bps"] is not None:
                    return last_result
            except Exception:
                log.exception("Avantis Risk API error for pair %s (attempt %d/%d)", pair_index, attempt + 1, max_retries)

        return last_result

    def _get_spread(self, pair_idx: str, pair_info: Dict, position_size: float, is_long: bool = True) -> Tuple[Optional[float], Dict]:
        """
        Fetch spread exclusively from Risk API v2 (SpreadController_getSpreadV2).
        Uses dynamicSpreadPct as the spread value directly — no fallback to spreadP.
        Returns (spread_bps, spread_metadata).
        spread_bps is None when the API returns null (e.g. OUT_OF_LIQUIDITY).
        """
        result = self._fetch_dynamic_spread(int(pair_idx), position_size, is_long, is_pnl=False)
        if result is not None:
            return result["spread_bps"], result
        return None, {"spread_bps": None, "is_dynamic_spread_enabled": False}

    def calculate_cost(self, asset_key: str, order_size_usd: float, is_long: bool = True) -> Optional[Dict]:
        result = self._get_pair_info(asset_key)
        if not result:
            return None
        pair_idx, pair_info = result

        max_wallet_oi = pair_info.get("maxWalletOI", float('inf'))
        pair_max_oi   = pair_info.get("pairMaxOI", 0)
        values        = pair_info.get("values", {})
        oi            = pair_info.get("openInterest", {})

        if is_long:
            per_side_cap = pair_max_oi * values.get("maxLongOiP", 100) / 100
            liq_cap      = max(0, per_side_cap - oi.get("long", 0))
        else:
            per_side_cap = pair_max_oi * values.get("maxShortOiP", 100) / 100
            liq_cap      = max(0, per_side_cap - oi.get("short", 0))

        max_order     = min(max_wallet_oi, liq_cap) if liq_cap > 0 else 0

        filled        = order_size_usd <= max_order
        filled_usd    = min(order_size_usd, max_order)
        unfilled_usd  = max(0, order_size_usd - max_order)
        position_size = filled_usd

        close_fee_bps           = pair_info.get("closeFeeP", 0) * 100
        max_lev                 = pair_info.get("leverages", {}).get("maxLeverage")

        open_fee_bps            = self._calculate_opening_fee(pair_info, position_size, is_long=is_long)
        spread_bps, spread_meta = self._get_spread(pair_idx, pair_info, position_size, is_long=is_long)

        if spread_bps is None:
            # The dynamic-spread Risk API refuses to quote sizes above the pair's
            # true fillable liquidity, which can be far below the OI-based cap
            # (e.g. BRENT quotes up to ~$1.4M despite an ~$8.7M OI cap). A fixed
            # percentage ladder off the cap can miss this entirely, so binary-search
            # for the largest size the API will actually quote and report it as a
            # partial fill. Assumes quotability is monotonic (quotes below a
            # threshold, null above), which matches observed API behaviour.
            hi          = position_size   # known to return null
            lo          = 0.0             # assumed quotable floor
            best_size   = None
            best_spread = None
            best_meta   = spread_meta
            for _ in range(self._MAX_SPREAD_SEARCH_ITERS):
                mid = (lo + hi) / 2
                if mid <= 0 or (hi - lo) / position_size < self._SPREAD_SEARCH_TOL:
                    break
                probe_bps, probe_meta = self._get_spread(pair_idx, pair_info, mid, is_long=is_long)
                if probe_bps is not None:
                    lo, best_size, best_spread, best_meta = mid, mid, probe_bps, probe_meta
                else:
                    hi = mid

            if best_size is not None:
                position_size = best_size
                filled_usd    = min(order_size_usd, best_size)
                unfilled_usd  = max(0, order_size_usd - best_size)
                filled        = False
                spread_bps    = best_spread
                spread_meta   = best_meta
                open_fee_bps  = self._calculate_opening_fee(pair_info, position_size, is_long=is_long)

        if spread_bps is None:
            filled = False

        total_cost = (spread_bps or 0) + open_fee_bps + close_fee_bps

        def _safe(v):
            return None if v == float('inf') else v

        return {
            'symbol':                    asset_key,
            'max_leverage':              max_lev,
            'executed':                  True if filled else 'PARTIAL',
            'mid_price':                 0,
            'slippage_bps':              spread_bps,
            'opening_slippage_bps':      spread_bps,
            'closing_slippage_bps':      0.0,
            'buy_slippage_bps':          spread_bps,
            'sell_slippage_bps':         0.0,
            'slippage_type':             'opening_closing',
            'open_fee_bps':              open_fee_bps,
            'close_fee_bps':             close_fee_bps,
            'maker_fee_bps':             open_fee_bps,
            'total_cost_bps':            total_cost,
            'filled':                    filled,
            'order_size_usd':            order_size_usd,
            'filled_usd':                filled_usd,
            'unfilled_usd':              unfilled_usd,
            'max_order_size':            _safe(max_order),
            'max_wallet_oi':             _safe(max_wallet_oi),
            'liquidity_cap':             _safe(liq_cap),
            'fixed_spread_bps':          spread_meta.get('fixed_spread_bps', 0.0),
            'price_impact_spread_bps':   spread_meta.get('price_impact_spread_bps', 0.0),
            'skew_impact_spread_bps':    spread_meta.get('skew_impact_spread_bps', 0.0),
            'is_dynamic_spread_enabled': spread_meta.get('is_dynamic_spread_enabled', False),
            'spread_error_code':         spread_meta.get('error_code'),
            'buy':  {'filled': filled, 'filled_usd': filled_usd, 'unfilled_usd': unfilled_usd, 'levels_used': 1, 'slippage_bps': spread_bps},
            'sell': {'filled': filled, 'filled_usd': filled_usd, 'unfilled_usd': unfilled_usd, 'levels_used': 1, 'slippage_bps': 0.0},
            'timestamp':                 time.time(),
        }
