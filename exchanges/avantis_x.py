#!/usr/bin/env python3
"""
exchanges/avantis_x.py - AvantisX hybrid integration.

Spread source : LMAX orderbook depth, stored in ClickHouse.
                Size-adjusted effective spread (buy VWAP – sell VWAP) with a
                +20 % safety buffer, identical to the "try avantis lmax" API.
Fee source    : Avantis socket API  (skewEqParams opening fee + closeFeeP).
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from typing import Dict, List, Optional

import clickhouse_connect

from exchanges.avantis import AvantisAPI

log = logging.getLogger(__name__)


class AvantisXAPI:
    """Size-adjusted LMAX spread combined with Avantis opening / closing fees. (AvantisX)"""

    # Asset key → name fragment used to look up lmax_instruments (case-insensitive LIKE)
    LMAX_INSTRUMENTS: Dict[str, str] = {
        "XAU":    "Gold (Spot)",
        "XAG":    "Silver (Spot)",
        "EURUSD": "EUR/USD",
        "GBPUSD": "GBP/USD",
        "USDJPY": "USD/JPY",
    }

    def __init__(self, avantis: Optional[AvantisAPI] = None):
        self._avantis = avantis or AvantisAPI()
        self._local = threading.local()
        self._instrument_cache: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # ClickHouse helpers
    # ------------------------------------------------------------------

    def _get_client(self):
        if not getattr(self._local, "ch", None):
            self._local.ch = clickhouse_connect.get_client(
                host=os.environ["CLICKHOUSE_HOST"],
                port=int(os.environ["CLICKHOUSE_PORT"]),
                username=os.environ["CLICKHOUSE_USER"],
                password=os.environ["CLICKHOUSE_PASSWORD"],
                database=os.environ["CLICKHOUSE_DB"],
                secure=os.environ["CLICKHOUSE_SECURE"].lower() == "true",
            )
        return self._local.ch

    def _query(self, sql: str, params: Optional[Dict] = None) -> List[Dict]:
        client = self._get_client()
        result = client.query(sql, parameters=params or {})
        return [dict(zip(result.column_names, row)) for row in result.result_rows]

    # ------------------------------------------------------------------
    # Instrument resolution
    # ------------------------------------------------------------------

    def _resolve_instrument(self, asset_key: str) -> Optional[Dict]:
        """Return lmax_instruments row for asset_key, cached after first lookup."""
        if asset_key in self._instrument_cache:
            return self._instrument_cache[asset_key]

        fragment = self.LMAX_INSTRUMENTS.get(asset_key.upper())
        if not fragment:
            return None

        try:
            rows = self._query(
                """
                SELECT id, name, symbol, contract_size
                FROM market_data.lmax_instruments
                WHERE name   = %(frag)s
                   OR symbol = %(frag)s
                LIMIT 1
                """,
                {"frag": fragment},
            )
        except Exception:
            log.exception("ClickHouse instrument lookup failed for %s", asset_key)
            return None

        if not rows:
            log.warning("LMAX: no instrument found for %s (name=%r)", asset_key, fragment)
            return None

        inst = rows[0]
        self._instrument_cache[asset_key] = inst
        return inst

    # ------------------------------------------------------------------
    # ClickHouse queries (ported from lmax_depth_tools.py)
    # ------------------------------------------------------------------

    def _get_tob(self, instrument_id: str) -> Optional[Dict]:
        """Latest top-of-book row (level=0, side=bid)."""
        try:
            rows = self._query(
                """
                SELECT mid, best_bid, best_ask, spread,
                       spread / nullIf(mid, 0) * 10000 AS spread_bps,
                       publish_ts
                FROM market_data.lmax_orderbook_levels
                WHERE instrument_id = %(id)s
                  AND level = 0
                  AND side = 'bid'
                ORDER BY publish_ts DESC
                LIMIT 1
                """,
                {"id": instrument_id},
            )
            return rows[0] if rows else None
        except Exception:
            log.exception("ClickHouse TOB query failed for instrument_id=%s", instrument_id)
            return None

    def _run_impact(
        self,
        instrument_id: str,
        side: str,           # 'ask' = simulating a buy; 'bid' = simulating a sell
        target_notional: float,
        mid: float,
        contract_size: float,
        publish_ts: object = None,
    ) -> Optional[Dict]:
        """Walk the orderbook on *side* for the given snapshot and compute VWAP + slippage.

        publish_ts pins the query to the same snapshot as the TOB fetch so that
        mid and the orderbook levels are always consistent.
        """
        try:
            rows = self._query(
                """
                SELECT price, quantity
                FROM market_data.lmax_orderbook_levels
                WHERE instrument_id = %(id)s
                  AND side = %(side)s
                  AND publish_ts = %(ts)s
                ORDER BY level ASC
                """,
                {"id": instrument_id, "side": side, "ts": publish_ts},
            )
        except Exception:
            log.exception("ClickHouse impact query failed for instrument_id=%s side=%s", instrument_id, side)
            return None

        if not rows:
            return None

        levels = sorted(rows, key=lambda r: float(r["price"]), reverse=(side == "bid"))

        cost_sum = 0.0
        qty_sum = 0.0
        filled_notional = 0.0
        total_book_notional = sum(
            float(r["price"]) * float(r["quantity"]) * contract_size for r in levels
        )

        for row in levels:
            price          = float(row["price"])
            qty            = float(row["quantity"])
            level_notional = price * qty * contract_size
            remaining      = target_notional - filled_notional

            if remaining <= 0:
                break

            take_notional    = min(level_notional, remaining)
            take_qty         = take_notional / (price * contract_size)
            cost_sum        += price * take_qty
            qty_sum         += take_qty
            filled_notional += take_notional

        if qty_sum == 0:
            return None

        avg_exec_price = cost_sum / qty_sum
        can_fill       = filled_notional >= target_notional * 0.999  # 0.1 % rounding tolerance

        if mid:
            slippage_bps = (
                (avg_exec_price - mid) / mid * 10000 if side == "ask"
                else (mid - avg_exec_price) / mid * 10000
            )
        else:
            slippage_bps = 0.0

        return {
            "avg_exec_price":       avg_exec_price,
            "slippage_bps":         slippage_bps,
            "can_fill":             can_fill,
            "filled_notional":      filled_notional,
            "total_book_notional":  total_book_notional,
        }

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
        Calculate total execution cost using LMAX spread + Avantis fees.

        Returns the same shape as AvantisAPI.calculate_cost() so it slots
        straight into the FeeComparator without special-casing.
        """
        # 1. Resolve LMAX instrument
        inst = self._resolve_instrument(asset_key)
        if inst is None:
            return None

        inst_id       = str(inst["id"])
        contract_size = float(inst["contract_size"])

        # 2. Top-of-book
        tob = self._get_tob(inst_id)
        if not tob:
            return None

        mid = float(tob["mid"])

        # 3. Size-adjusted market impact — both sides pinned to the same snapshot.
        #    Formatted as a string because ClickHouse Connect doesn't bind
        #    datetime objects correctly for DateTime64 equality comparisons.
        raw_ts = tob["publish_ts"]
        ts     = raw_ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{raw_ts.microsecond // 1000:03d}"
        buy_impact  = self._run_impact(inst_id, "ask", order_size_usd, mid, contract_size, publish_ts=ts)
        sell_impact = self._run_impact(inst_id, "bid", order_size_usd, mid, contract_size, publish_ts=ts)

        if not buy_impact or not sell_impact:
            return None

        # 4. Effective spread = buy VWAP – sell VWAP, expressed in bps
        #    (+20 % buffer, same as the LMAX API)
        eff_spread       = buy_impact["avg_exec_price"] - sell_impact["avg_exec_price"]
        eff_spread_bps   = eff_spread / mid * 10000 if mid else 0.0
        adj_spread_bps   = eff_spread_bps * 1.20

        buy_slip  = buy_impact["slippage_bps"]  * 1.20
        sell_slip = sell_impact["slippage_bps"] * 1.20
        filled    = buy_impact["can_fill"] and sell_impact["can_fill"]

        # 5. Avantis opening / closing fees + holding fee
        av_result = self._avantis._get_pair_info(asset_key)
        if av_result:
            pair_idx, pair_info = av_result
            open_fee_bps  = self._avantis._calculate_opening_fee(pair_info, order_size_usd, is_long=is_long)
            close_fee_bps = pair_info.get("closeFeeP", 0) * 100

            margin_fee            = pair_info.get("marginFee", {})
            holding_1h_long_pct   = margin_fee.get("long",  0)
            holding_1h_short_pct  = margin_fee.get("short", 0)
            holding_24h_long_pct  = math.floor(holding_1h_long_pct  * 24 * 1_000_000) / 1_000_000
            holding_24h_short_pct = math.floor(holding_1h_short_pct * 24 * 1_000_000) / 1_000_000

            max_wallet_oi = pair_info.get("maxWalletOI", float("inf"))
            max_lev       = pair_info.get("leverages", {}).get("maxLeverage")
        else:
            open_fee_bps  = 0.0
            close_fee_bps = 0.0
            holding_1h_long_pct   = 0.0
            holding_1h_short_pct  = 0.0
            holding_24h_long_pct  = 0.0
            holding_24h_short_pct = 0.0
            max_wallet_oi = float("inf")
            max_lev       = None

        # slippage_bps = adj_spread_bps (full round-trip, not halved).
        # The comparator special-cases 'avantis' to avoid doubling; avantis_x
        # is added to the same set so the same logic applies.
        total_cost = adj_spread_bps + open_fee_bps + close_fee_bps

        filled_usd   = min(buy_impact["filled_notional"], sell_impact["filled_notional"])
        unfilled_usd = max(0.0, order_size_usd - filled_usd)

        return {
            "symbol":               inst["name"],
            "max_leverage":         max_lev,
            "executed":             True if filled else "PARTIAL",
            "mid_price":            mid,
            "slippage_bps":         adj_spread_bps,
            "opening_slippage_bps": buy_slip  if is_long else sell_slip,
            "closing_slippage_bps": sell_slip if is_long else buy_slip,
            "buy_slippage_bps":     buy_slip,
            "sell_slippage_bps":    sell_slip,
            "slippage_type":        "opening_closing",
            "open_fee_bps":         open_fee_bps,
            "close_fee_bps":        close_fee_bps,
            "maker_fee_bps":        open_fee_bps,
            "total_cost_bps":       total_cost,
            "filled":               filled,
            "order_size_usd":       order_size_usd,
            "filled_usd":           filled_usd,
            "unfilled_usd":         unfilled_usd,
            "max_wallet_oi":        max_wallet_oi,
            "lmax_tob_spread_bps":  float(tob["spread_bps"]),
            "lmax_eff_spread_bps":  eff_spread_bps,
            "lmax_adj_spread_bps":  adj_spread_bps,
            "lmax_instrument":      inst["name"],
            "buy":  {
                "filled":               buy_impact["can_fill"],
                "filled_usd":           buy_impact["filled_notional"],
                "slippage_bps":         buy_slip,
                "total_book_notional":  buy_impact["total_book_notional"],
            },
            "sell": {
                "filled":               sell_impact["can_fill"],
                "filled_usd":           sell_impact["filled_notional"],
                "slippage_bps":         sell_slip,
                "total_book_notional":  sell_impact["total_book_notional"],
            },
            "holding_fee_1h_long_pct":   round(holding_1h_long_pct,   6),
            "holding_fee_1h_short_pct":  round(holding_1h_short_pct,  6),   
            "holding_fee_24h_long_pct":  holding_24h_long_pct,
            "holding_fee_24h_short_pct": holding_24h_short_pct,
            "timestamp": time.time(),
        }
