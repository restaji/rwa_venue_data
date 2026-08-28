#!/usr/bin/env python3
"""
exchanges/edgex.py - EdgeX exchange integration.

Covers: orderbook-based slippage, taker/maker fees, max leverage (tier 1),
and 4H funding rate (× 6 for 24H).

Public APIs used (v2 — the v1 host still answers but serves a stale, delisted
USD-quoted contract set with permanently empty books; live markets are the
USDC-quoted contracts on the v2 host):
  Metadata:  GET https://edgex-prod-v2.edgex.exchange/api/v2/public/meta/getMetaData
  Orderbook: GET https://edgex-prod-v2.edgex.exchange/api/v2/public/quote/getDepth?contractId=X&level=200
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

from models import StandardizedOrderbook, ExecutionCalculator, floor_leverage


class EdgeXAPI:
    """Client for EdgeX exchange orderbook and fee data."""

    BASE_URL    = "https://edgex-prod-v2.edgex.exchange/api/v2/public"
    DEPTH_URL   = f"{BASE_URL}/quote/getDepth"
    META_URL    = f"{BASE_URL}/meta/getMetaData"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept":       "application/json",
        })
        # contract_id -> {taker_fee_bps, maker_fee_bps, max_leverage}
        self.meta_cache: Dict = {}
        self._load_meta_cache()

    # ------------------------------------------------------------------
    # Metadata cache (fees + leverage from contract list)
    # ------------------------------------------------------------------
    def _load_meta_cache(self) -> None:
        """Load fees and max leverage for all contracts at startup."""
        try:
            resp = self.session.get(self.META_URL, timeout=30)
            if resp.status_code != 200:
                return
            contracts = resp.json().get('data', {}).get('contractList', [])
            for c in contracts:
                cid = str(c.get('contractId', ''))
                if not cid:
                    continue

                taker_fee_bps = float(c.get('defaultTakerFeeRate', 0)) * 10_000
                maker_fee_bps = float(c.get('defaultMakerFeeRate', 0)) * 10_000

                # Tier 1 = lowest position size = highest allowed leverage
                risk_tiers = c.get('riskTierList', [])
                max_lev = None
                if risk_tiers:
                    try:
                        max_lev = floor_leverage(risk_tiers[0].get('maxLeverage'))
                    except (TypeError, ValueError):
                        pass

                self.meta_cache[cid] = {
                    'taker_fee_bps': taker_fee_bps,
                    'maker_fee_bps': maker_fee_bps,
                    'max_leverage':  max_lev,
                    'contract_name': c.get('contractName', ''),
                }
        except Exception:
            log.exception("EdgeX meta cache load error")

    def get_fees(self, contract_id: int) -> Tuple[Optional[float], Optional[float]]:
        """Return (taker_fee_bps, maker_fee_bps) for a contract."""
        entry = self.meta_cache.get(str(contract_id), {})
        return entry.get('taker_fee_bps'), entry.get('maker_fee_bps')

    def get_max_leverage(self, contract_id: int) -> Optional[int]:
        """Return tier-1 max leverage for a contract, floored to a whole number."""
        return floor_leverage(self.meta_cache.get(str(contract_id), {}).get('max_leverage'))

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, contract_id: int) -> Optional[StandardizedOrderbook]:
        """
        Fetch depth (level=200 which is the maximum level) and return a StandardizedOrderbook.

        """
        try:
            params = {'contractId': contract_id, 'level': 200}
            resp   = self.session.get(self.DEPTH_URL, params=params, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get('code') != 'SUCCESS':
                return None

            entries = data.get('data', [])
            if not entries:
                return None
            book = entries[0]

            raw_asks = book.get('asks', [])
            raw_bids = book.get('bids', [])
            if not raw_asks or not raw_bids:
                return None

            best_ask = float(raw_asks[0]['price'])
            best_bid = float(raw_bids[0]['price'])
            mid      = (best_ask + best_bid) / 2

            asks = [{'price': float(a['price']), 'qty': float(a['size'])} for a in raw_asks]
            bids = [{'price': float(b['price']), 'qty': float(b['size'])} for b in raw_bids]

            return StandardizedOrderbook(
                bids=bids,
                asks=asks,
                best_bid=best_bid,
                best_ask=best_ask,
                mid_price=mid,
                timestamp=time.time(),
            )
        except Exception:
            log.exception("EdgeX orderbook error for contractId=%s", contract_id)
            return None

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(
        self,
        contract_id: int,
        order_size_usd: float,
        direction: str = 'long',
        symbol: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Calculate execution cost for a given order size.
        Returns a standardized result dict compatible with comparator.py.

        Slippage is measured from mid-price ((best_bid + best_ask) / 2),
        consistent with all other orderbook-based venues.

        For a long: opening = buy slippage, closing = sell slippage.
        For a short: opening = sell slippage, closing = buy slippage.
        """
        ob = self.get_orderbook(contract_id)
        if not ob:
            return None

        taker_fee_bps, maker_fee_bps = self.get_fees(contract_id)
        if taker_fee_bps is None:
            return None

        max_lev = self.get_max_leverage(contract_id)
        result  = ExecutionCalculator.calculate_execution_cost(ob, order_size_usd, taker_fee_bps, taker_fee_bps)

        if not result:
            return None

        buy_slip_bps  = result.get('buy_slippage_bps', 0.0)
        sell_slip_bps = result.get('sell_slippage_bps', 0.0)

        is_long = direction.lower() == 'long'
        result['opening_slippage_bps'] = buy_slip_bps  if is_long else sell_slip_bps
        result['closing_slippage_bps'] = sell_slip_bps if is_long else buy_slip_bps
        result['slippage_type']        = 'opening_closing'
        result['total_cost_bps']       = buy_slip_bps + sell_slip_bps + taker_fee_bps * 2
        result['symbol']       = symbol
        result['max_leverage'] = max_lev
        result['maker_fee_bps'] = maker_fee_bps
        return result
