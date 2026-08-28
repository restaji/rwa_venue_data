#!/usr/bin/env python3
"""
exchanges/avantis.py - Avantis exchange integration.

Spread numbers come from the Avantis Risk v2 API
(https://prod-api.avantisfi.com/risk/v2), which is the same service the
protocol itself uses at quote/execution time. Pair metadata (fees, leverage,
OI caps, coin-denominated OI for USD→coin conversion) still comes from the
public trading API (https://data.avantisfi.com/v2/trading).

If the Risk v2 API is unreachable, we transparently fall back to the legacy
in-process spread formula so quotes still resolve.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Dict, List, Optional, Tuple

import requests

from models import floor_leverage

log = logging.getLogger(__name__)


class AvantisAPI:
    """Dynamic fee and spread calculation for Avantis, backed by Risk v2."""

    TRADING_API = "https://data.avantisfi.com/v2/trading"
    RISK_API    = "https://prod-api.avantisfi.com/risk/v2"

    # Risk API is a POST endpoint that requires a checksummed EIP-55 address.
    # The trader identity does not influence the base spread (`spreadPctWithoutFlow10`)
    # — it only affects flow-fee bypass which we intentionally ignore below.
    DEFAULT_TRADER = os.environ.get(
        "AVANTIS_RISK_TRADER",
        "0x0FaE768Ef2191fB74968cd1e5D14EBd35b90Ff85",
    )

    # orderType 0 = MARKET (taker). Other values (1..4, -1) map to limit / TP-SL /
    # liquidation flows; keep this at MARKET since the comparator quotes taker fills.
    ORDER_TYPE_MARKET = 0

    # Avantis pair indices for all RWA (real-world asset) instruments.
    # Sourced from https://data.avantisfi.com/v2/trading — groupIndex 2 (forex),
    # 3 (commodities), and 6 (equities/indices). Every entry here must have
    # `isPairListed=True`; `_get_pair_info` enforces this at runtime so any
    # future silent delisting (e.g. Avantis reusing pair 92 from SPCX → ZEC)
    # is filtered out instead of quoting the wrong asset.
    #
    # Delisted (removed): USDTRY(52), USDINR(67), USDKRW(68), USDMXN(69),
    # USDZAR(70), USDBRL(73), USDIDR(74), USDTWD(77), USDSEK(16 — force-close
    # announced for Monday). Also memecoins/exotics at idx 22–58 (never
    # supported here).
    PAIRS = {
        # Commodities (group 3)
        "XAU": 21, "XAG": 20, "WTI": 96, "BRENT": 97,

        # Forex (group 2). Keys are normalized to <BASE><QUOTE>, matching how
        # the other venues (Ostium, Lighter, Aster) surface these pairs.
        "EURUSD": 11, "USDJPY": 12, "GBPUSD": 13,
        "USDCAD": 14, "USDCHF": 15,
        "AUDUSD": 17, "NZDUSD": 18, "USDSGD": 19,
        "USDCNH": 66,

        # Indices (group 6). Pair 78/79 are US500 / US100 (S&P 500 / Nasdaq-100),
        # matching Ostium SPX/NDX, HL xyz SP500/XYZ100, and Extended SPX500m/TECH100m.
        "US500": 78, "US100": 79,

        # Equities (group 6). NB: prior versions of this map had SPCX=92, which
        # is actually ZEC (group 1). SPCX moved to 105.
        "COIN": 80, "NVDA": 81, "AAPL": 82, "AMZN": 83, "MSFT": 84,
        "META": 85, "TSLA": 86, "GOOG": 87, "HOOD": 91, "SPCX": 105,
        "AMD": 98, "INTC": 99, "MU": 100, "SNDK": 101, "CBRS": 102,
        "EWY": 103, "BABA": 104, "CRCL": 106, "MRVL": 107, "MSTR": 108,
        "BB": 109, "PLTR": 110, "AVGO": 111, "NFLX": 112, "CRWV": 113,
        "SKHY": 114,
    }

    def __init__(self):
        self._pair_data      = None
        self._group_info     = None
        self._last_fetch     = 0.0
        self._cache_ttl      = 30

        self._snapshots      : Dict[int, List[Dict]] = {}
        self._snapshots_at   = 0.0
        self._snapshots_ttl  = 5

        self._session        = requests.Session()

    # ------------------------------------------------------------------
    # Trading API (pair metadata)
    # ------------------------------------------------------------------
    def _fetch_socket_data(self):
        now = time.time()
        if self._pair_data and (now - self._last_fetch) < self._cache_ttl:
            return
        try:
            resp    = self._session.get(self.TRADING_API, timeout=30)
            payload = resp.json()
            data    = payload if "pairInfos" in payload else payload.get("data", {})
            self._pair_data  = data.get("pairInfos", {})
            self._group_info = data.get("groupInfo", {})
            self._last_fetch = now
        except Exception:
            log.exception("Avantis trading API error")
            self._pair_data  = self._pair_data  or {}
            self._group_info = self._group_info or {}

    def _get_pair_info(self, asset_key: str) -> Optional[Tuple[str, Dict]]:
        """
        Resolve `asset_key` to a live pair on Avantis. Returns None if:
          - The symbol isn't in our PAIRS map.
          - The trading API doesn't return that pair index.
          - The pair is currently `isPairListed=False` (Avantis delisted it and
            may reuse the index for a different asset — like SPCX(92) → ZEC).
        """
        self._fetch_socket_data()
        pair_idx = self.PAIRS.get(asset_key.upper())
        if pair_idx is None:
            return None
        pair_info = self._pair_data.get(str(pair_idx))
        if not pair_info:
            return None
        if not pair_info.get("isPairListed", False):
            log.info(
                "Avantis pair %s (idx=%s) is no longer listed (from=%s to=%s); skipping",
                asset_key, pair_idx, pair_info.get("from"), pair_info.get("to"),
            )
            return None
        return str(pair_idx), pair_info

    # ------------------------------------------------------------------
    # Risk v2: orderbook snapshots (per-source depth, cached)
    # ------------------------------------------------------------------
    def _fetch_snapshots(self) -> None:
        now = time.time()
        if self._snapshots and (now - self._snapshots_at) < self._snapshots_ttl:
            return
        try:
            resp = self._session.get(f"{self.RISK_API}/orderbook/snapshots", timeout=10)
            resp.raise_for_status()
            by_pair: Dict[int, List[Dict]] = {}
            for row in resp.json():
                by_pair.setdefault(int(row["pairIndex"]), []).append({
                    "source":   row.get("source"),
                    "bid_coin": float(row.get("cumulativeCoinLiquidityBid") or 0),
                    "ask_coin": float(row.get("cumulativeCoinLiquidityAsk") or 0),
                    "age_ms":   int(row.get("ageMs") or 0),
                })
            self._snapshots    = by_pair
            self._snapshots_at = now
        except Exception:
            log.warning("Avantis risk snapshots fetch failed", exc_info=True)
            # keep the previous cache; do not clobber

    def _snapshots_for(self, pair_idx: int) -> List[Dict]:
        self._fetch_snapshots()
        return self._snapshots.get(pair_idx, [])

    # ------------------------------------------------------------------
    # Price / size helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _implied_coin_price(pair_info: Dict) -> Optional[float]:
        """
        Derive coin mid-price from USD-denominated OI / coin-denominated OI.
        Returns None when the pair has no open interest (rare — new listings only).
        """
        oi = pair_info.get("openInterest") or {}
        co = pair_info.get("coinOI") or {}
        usd_total  = float(oi.get("long") or 0) + float(oi.get("short") or 0)
        coin_total = float(co.get("long") or 0) + float(co.get("short") or 0)
        if coin_total <= 0 or usd_total <= 0:
            return None
        return usd_total / coin_total

    def _taker_fees_bps(self, pair_info: Dict) -> Tuple[float, float]:
        """
        Open/close fees come only from `additionalPairParams2.openTakerFeeP` and
        `closeTakerFeeP` (percent → bps). Do not use top-level `openFeeP` /
        `closeFeeP` (list/reference rates) or maker-fee fields.
        """
        extra = pair_info.get("additionalPairParams2") or {}
        open_bps  = float(extra.get("openTakerFeeP")  or 0) * 100
        close_bps = float(extra.get("closeTakerFeeP") or 0) * 100
        return open_bps, close_bps

    # ------------------------------------------------------------------
    # Risk v2: spread
    # ------------------------------------------------------------------
    def _call_risk_spread(
        self,
        pair_idx: int,
        coin_size: float,
        is_long: bool,
        is_open: bool,
    ) -> Optional[Dict]:
        """
        POST /risk/v2/spread.  Returns the parsed body on success, None on any error.
        """
        if coin_size <= 0:
            return None
        try:
            body = {
                "pairIndex":  int(pair_idx),
                "trader":     self.DEFAULT_TRADER,
                "coinSize10": str(int(coin_size * 1e10)),
                "isLong":     bool(is_long),
                "isOpen":     bool(is_open),
                "orderType":  self.ORDER_TYPE_MARKET,
            }
            resp = self._session.post(f"{self.RISK_API}/spread", json=body, timeout=5)
            if resp.status_code >= 400:
                log.debug("risk /spread %s -> %d: %s", body, resp.status_code, resp.text[:200])
                return None
            return resp.json()
        except Exception:
            log.warning("Avantis risk /spread call failed", exc_info=True)
            return None

    @staticmethod
    def _pct10_to_bps(raw: Optional[str]) -> float:
        """
        spreadPct in 10-decimal precision → basis points.
        The value is *percent* scaled by 1e10 (i.e. same unit family as the trading
        API's `spreadP`, which is percent). So: raw / 1e10 = percent, * 100 → bps,
        collapsing to raw / 1e8.  Cross-checked against the Avantis frontend
        tooltip ("0.0128% / 1.28 bps" for a $1M XAU long → raw ≈ 128_000_000).
        """
        if raw is None:
            return 0.0
        try:
            return float(raw) / 1e8
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    # Legacy (fallback) spread math — unchanged from the pre-Risk-API version
    # ------------------------------------------------------------------
    def _price_impact_spread_bps(self, pair_info: Dict, position_size: float, is_long: bool) -> float:
        params = pair_info.get("pairParams") or {}
        depth  = float(params.get("onePercentDepthAbove" if is_long else "onePercentDepthBelow") or 0)
        param  = float(pair_info.get("priceImpactMultiplier") or 0)
        if position_size <= 0 or depth <= 0 or param <= 0:
            return 0.0
        linear_pct = position_size / depth
        exp_pct    = math.exp(min(linear_pct / param, 20.0)) - 1
        return max(exp_pct, linear_pct) * 100

    def _skew_impact_spread_bps(self, pair_info: Dict, position_size: float, is_long: bool) -> float:
        param = float(pair_info.get("skewImpactMultiplier") or 0)
        if param <= 0 or position_size <= 0:
            return 0.0
        oi       = pair_info.get("openInterest") or {}
        long_oi  = float(oi.get("long")  or 0)
        short_oi = float(oi.get("short") or 0)
        total    = long_oi + short_oi
        s_before = (long_oi / total) if total > 0 else 0.5
        s_after  = (
            (long_oi + position_size) / (total + position_size)
            if is_long else
            long_oi / (total + position_size)
        )
        delta = (
            math.exp(s_after) - math.exp(s_before)
            + math.exp(1 - s_after) - math.exp(1 - s_before)
        )
        return param * delta * 100

    def _legacy_spread(self, pair_info: Dict, position_size: float, is_long: bool) -> Tuple[float, float, float]:
        """
        Legacy fallback that mirrors the pre-Risk-API implementation.
        Returns (fixed_bps, price_impact_bps, skew_impact_bps) — the caller applies the cap.
        """
        fixed_bps  = float(pair_info.get("spreadP") or 0) * 100
        impact_bps = self._price_impact_spread_bps(pair_info, position_size, is_long)
        skew_bps   = self._skew_impact_spread_bps(pair_info, position_size, is_long)
        return fixed_bps, impact_bps, skew_bps

    # ------------------------------------------------------------------
    # Spread orchestration
    # ------------------------------------------------------------------
    def _get_spread(
        self,
        pair_idx: int,
        pair_info: Dict,
        position_size_usd: float,
        is_long: bool = True,
    ) -> Tuple[float, Dict]:
        """
        Prefer the Risk v2 API. Fall back to the legacy in-process formula on any
        error, or when we can't convert USD → coin size (missing implied price).
        """
        fixed_bps = float(pair_info.get("spreadP") or 0) * 100
        cap_pct   = float((pair_info.get("storagePairParams") or {}).get("posSpreadCap") or 0)
        cap_bps   = cap_pct * 100 if cap_pct > 0 else float("inf")

        group_index = pair_info.get("groupIndex", 0)
        group       = (self._group_info or {}).get(str(group_index), {})
        group_dyn   = bool(group.get("isSpreadDynamic", False))

        coin_price = self._implied_coin_price(pair_info)
        coin_size  = (position_size_usd / coin_price) if (coin_price and coin_price > 0) else 0.0

        # ---- Path 1: Risk v2 API (authoritative) ----
        # The risk service is the same code the contract calls at execution; it
        # already applies its own floors/caps, so we do NOT re-clamp with cap_bps here.
        risk = self._call_risk_spread(pair_idx, coin_size, is_long, True) if coin_size > 0 else None
        if risk is not None:
            total_bps      = self._pct10_to_bps(risk.get("spreadPctWithoutFlow10"))
            with_flow_bps  = self._pct10_to_bps(risk.get("estimatedSpreadPctWithFlow10"))
            spread_bps     = total_bps
            dynamic_bps    = max(0.0, spread_bps - fixed_bps)
            return spread_bps, {
                "spread_bps":                spread_bps,
                "fixed_spread_bps":          fixed_bps,
                "dynamic_spread_bps":        dynamic_bps,
                "price_impact_spread_bps":   dynamic_bps,   # kept for schema compat
                "skew_impact_spread_bps":    0.0,
                "is_dynamic_spread_enabled": group_dyn or dynamic_bps > 0,
                "spread_source":             "risk_v2",
                "spread_mechanism":          risk.get("spreadMechanism"),
                "spread_bypass":             bool(risk.get("byPass", False)),
                "estimated_spread_with_flow_bps": with_flow_bps,
                "coin_size":                 coin_size,
                "coin_price":                coin_price,
            }

        # ---- Path 2: legacy in-process fallback ----
        fixed, impact, skew = self._legacy_spread(pair_info, position_size_usd, is_long)
        spread_bps          = min(fixed + impact + skew, cap_bps)
        return spread_bps, {
            "spread_bps":                spread_bps,
            "fixed_spread_bps":          fixed,
            "dynamic_spread_bps":        impact + skew,
            "price_impact_spread_bps":   impact,
            "skew_impact_spread_bps":    skew,
            "is_dynamic_spread_enabled": group_dyn or impact > 0 or skew > 0,
            "spread_source":             "legacy_fallback",
            "spread_mechanism":          None,
            "spread_bypass":             False,
            "estimated_spread_with_flow_bps": spread_bps,
            "coin_size":                 coin_size,
            "coin_price":                coin_price,
        }

    # ------------------------------------------------------------------
    # Public: cost calculation
    # ------------------------------------------------------------------
    def calculate_cost(self, asset_key: str, order_size_usd: float, is_long: bool = True) -> Optional[Dict]:
        result = self._get_pair_info(asset_key)
        if not result:
            return None
        pair_idx_str, pair_info = result
        pair_idx = int(pair_idx_str)

        max_wallet_oi = pair_info.get("maxWalletOI", float("inf"))
        pair_max_oi   = pair_info.get("pairMaxOI", 0)
        values        = pair_info.get("values", {})
        oi            = pair_info.get("openInterest", {})

        if is_long:
            per_side_cap = pair_max_oi * values.get("maxLongOiP", 100) / 100
            liq_cap      = max(0, per_side_cap - oi.get("long", 0))
        else:
            per_side_cap = pair_max_oi * values.get("maxShortOiP", 100) / 100
            liq_cap      = max(0, per_side_cap - oi.get("short", 0))

        max_order     = min(max_wallet_oi, liq_cap) if liq_cap > 0 else 0.0
        filled        = order_size_usd <= max_order and max_order > 0
        filled_usd    = min(order_size_usd, max_order) if max_order > 0 else 0.0
        unfilled_usd  = max(0.0, order_size_usd - filled_usd)
        position_size = filled_usd

        open_fee_bps, close_fee_bps = self._taker_fees_bps(pair_info)
        max_lev     = pair_info.get("leverages", {}).get("maxLeverage")

        spread_bps, spread_meta = self._get_spread(pair_idx, pair_info, position_size, is_long=is_long)
        total_cost = spread_bps + open_fee_bps + close_fee_bps

        def _safe(v):
            return None if v == float("inf") else v

        # Use Avantis' own ticker in the response (e.g. "US500/USD", "US100/USD")
        # rather than the caller's asset_key.
        av_from = pair_info.get("from")
        av_to   = pair_info.get("to")
        display_symbol = f"{av_from}/{av_to}" if av_from and av_to else asset_key

        return {
            "symbol":                    display_symbol,
            "max_leverage":              floor_leverage(max_lev),
            "executed":                  True if filled else "PARTIAL",
            "mid_price":                 spread_meta.get("coin_price") or 0,
            "slippage_bps":              spread_bps,
            "opening_slippage_bps":      spread_bps,
            "closing_slippage_bps":      0.0,
            "buy_slippage_bps":          spread_bps if is_long else 0.0,
            "sell_slippage_bps":         spread_bps if not is_long else 0.0,
            "slippage_type":             "opening_closing",
            "open_fee_bps":              open_fee_bps,
            "close_fee_bps":             close_fee_bps,
            "maker_fee_bps":             open_fee_bps,
            "total_cost_bps":            total_cost,
            "filled":                    filled,
            "order_size_usd":            order_size_usd,
            "filled_usd":                filled_usd,
            "unfilled_usd":              unfilled_usd,
            "max_order_size":            _safe(max_order),
            "max_wallet_oi":             _safe(max_wallet_oi),
            "liquidity_cap":             _safe(liq_cap),

            # Spread breakdown — same field names as before, plus richer risk-v2 fields
            "fixed_spread_bps":              spread_meta.get("fixed_spread_bps", 0.0),
            "dynamic_spread_bps":            spread_meta.get("dynamic_spread_bps", 0.0),
            "price_impact_spread_bps":       spread_meta.get("price_impact_spread_bps", 0.0),
            "skew_impact_spread_bps":        spread_meta.get("skew_impact_spread_bps", 0.0),
            "is_dynamic_spread_enabled":     spread_meta.get("is_dynamic_spread_enabled", False),
            "spread_source":                 spread_meta.get("spread_source"),
            "spread_mechanism":              spread_meta.get("spread_mechanism"),
            "spread_bypass":                 spread_meta.get("spread_bypass", False),
            "estimated_spread_with_flow_bps":spread_meta.get("estimated_spread_with_flow_bps"),

            # Per-source depth from risk/v2/orderbook/snapshots (cached).
            "orderbook_snapshots":       self._snapshots_for(pair_idx),

            "buy":  {"filled": filled, "filled_usd": filled_usd, "unfilled_usd": unfilled_usd, "levels_used": 1, "slippage_bps": spread_bps if is_long else 0.0},
            "sell": {"filled": filled, "filled_usd": filled_usd, "unfilled_usd": unfilled_usd, "levels_used": 1, "slippage_bps": spread_bps if not is_long else 0.0},
            "timestamp":                 time.time(),
        }
