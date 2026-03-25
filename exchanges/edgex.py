#!/usr/bin/env python3
"""
exchanges/edgex.py - EdgeX exchange integration.

Covers: orderbook-based slippage, taker/maker fees, max leverage (tier 1),
and 4H funding rate (× 6 for 24H).

Public APIs used:
  Metadata:  GET https://pro.edgex.exchange/api/v1/public/meta/getMetaData
  Orderbook: GET https://pro.edgex.exchange/api/v1/public/quote/getDepth?contractId=X&level=200
  Funding:   GET https://pro.edgex.exchange/api/v1/public/funding/getFundingRatePage
                   ?filterSettlementFundingRate=true&contractId=X&size=1
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

from models import StandardizedOrderbook, ExecutionCalculator


class EdgeXAPI:
    """Client for EdgeX exchange orderbook and fee data."""

    BASE_URL    = "https://pro.edgex.exchange/api/v1/public"
    FUNDING_URL = f"{BASE_URL}/funding/getFundingRatePage"
    DEPTH_URL   = f"{BASE_URL}/quote/getDepth"
    META_URL    = f"{BASE_URL}/meta/getMetaData"

    # Funding is 4H interval — multiply by 6 to get 24H
    FUNDING_INTERVALS_PER_DAY = 6

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
                        max_lev = float(risk_tiers[0].get('maxLeverage', 0)) or None
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

    def get_max_leverage(self, contract_id: int) -> Optional[float]:
        """Return tier-1 max leverage for a contract."""
        return self.meta_cache.get(str(contract_id), {}).get('max_leverage')

    # ------------------------------------------------------------------
    # Funding rate
    # ------------------------------------------------------------------
    def get_funding_rate_24h(self, contract_id: int) -> Optional[float]:
        """
        Return the 24H funding rate as a percentage of notional.
        Fetches the latest 4H settlement rate and multiplies by 6.
        """
        try:
            params = {
                'filterSettlementFundingRate': 'true',
                'contractId': contract_id,
                'size': 1,
            }
            resp = self.session.get(self.FUNDING_URL, params=params, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get('code') != 'SUCCESS':
                return None
            rows = data.get('data', {}).get('dataList', [])
            if not rows:
                return None
            rate_4h = float(rows[0].get('fundingRate', 0))
            return rate_4h * self.FUNDING_INTERVALS_PER_DAY * 100  # as %
        except Exception:
            log.exception("EdgeX funding rate error for contractId=%s", contract_id)
            return None

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

        Slippage is measured from best_ask (buy) / best_bid (sell) — matching
        EdgeX UI, which shows price impact beyond the best quote only.

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

        # Recalculate slippage from best_ask/best_bid (not mid) to match EdgeX UI
        best_ask = ob.best_ask
        best_bid = ob.best_bid
        buy_avg  = result.get('buy',  {}).get('avg_price', best_ask)
        sell_avg = result.get('sell', {}).get('avg_price', best_bid)

        buy_slip_bps  = abs(buy_avg  - best_ask) / best_ask * 10_000 if best_ask > 0 else 0.0
        sell_slip_bps = abs(best_bid - sell_avg) / best_bid * 10_000 if best_bid > 0 else 0.0

        result['buy_slippage_bps']  = buy_slip_bps
        result['sell_slippage_bps'] = sell_slip_bps
        result['slippage_bps']      = (buy_slip_bps + sell_slip_bps) / 2

        is_long = direction.lower() == 'long'
        result['opening_slippage_bps'] = buy_slip_bps  if is_long else sell_slip_bps
        result['closing_slippage_bps'] = sell_slip_bps if is_long else buy_slip_bps
        result['slippage_type']        = 'opening_closing'
        result['total_cost_bps']       = buy_slip_bps + sell_slip_bps + taker_fee_bps * 2
        result['symbol']               = symbol

        funding_24h = self.get_funding_rate_24h(contract_id)

        result['max_leverage']              = max_lev
        result['maker_fee_bps']             = maker_fee_bps
        result['funding_rate_4h_pct']       = (funding_24h / 6) if funding_24h is not None else None
        result['funding_rate_24h_pct']      = funding_24h
        result['holding_fee_24h_long_pct']  = funding_24h
        result['holding_fee_24h_short_pct'] = (-funding_24h if funding_24h is not None else None)
        return result
