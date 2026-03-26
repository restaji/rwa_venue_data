#!/usr/bin/env python3
"""
exchanges/ostium.py - Ostium exchange integration.

Covers: orderbook (synthetic/oracle-based), fees, max leverage,
and dynamic spread calculation (Pade-approximation volume decay).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, Optional, Tuple

import requests

from models import StandardizedOrderbook, ExecutionCalculator

log = logging.getLogger(__name__)


class OstiumAPI:
    """Client for interacting with Ostium's REST API with dynamic spread calculation."""

    BASE_URL  = "https://metadata-backend.ostium.io"
    PAIRS_URL = "https://app.ostium.com/api/pairs"
    ARB_RPC   = "https://arb1.arbitrum.io/rpc"

    # Precision constants for Solidity-compatible calculations
    PRECISION_27 = 10 ** 27
    PRECISION_18 = 10 ** 18
    PRECISION_10 = 10 ** 10

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept":        "application/json",
        })

        self._blocks_per_day: Optional[float] = None
        self._blocks_per_day_fetched: float   = 0.0
        self._blocks_per_day_ttl: float       = 300.0  # re-fetch every 5 min

        # Load metadata from Ostium pairs API (no fallbacks)
        self.metadata_cache = self._load_cache()

    # ------------------------------------------------------------------
    # Fees & metadata
    # ------------------------------------------------------------------
    def _load_cache(self) -> Dict:
        """
        Load fee, leverage, and dynamic spread metadata from Ostium `pairs` API.
        Also fetches 'seasons' data to override fees with 'newFee' if applicable.
        """
        cache: Dict = {}
        pair_id_map: Dict = {}   # id -> symbol

        # 1. Load Pairs (Base Metadata)
        try:
            response = self.session.get(self.PAIRS_URL, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    for pair in data:
                        base  = pair.get('from')
                        quote = pair.get('to')
                        p_id  = pair.get('id')
                        if not base or not quote:
                            continue
                        symbol = f"{base}{quote}"

                        if p_id is not None:
                            pair_id_map[p_id] = symbol

                        maker_fee_p = pair.get('makerFeeP')
                        taker_fee_p = pair.get('takerFeeP')
                        group       = pair.get('group') or {}

                        pair_max_lev  = pair.get('maxLeverage')
                        maker_max_lev = pair.get('makerMaxLeverage')
                        group_max_lev = group.get('maxLeverage')

                        max_lev = None
                        for lev_val in [pair_max_lev, maker_max_lev, group_max_lev]:
                            if lev_val is not None:
                                try:
                                    lev_float = float(lev_val)
                                    if lev_float > 0:
                                        max_lev = lev_float
                                        break
                                except (TypeError, ValueError):
                                    continue

                        if max_lev is not None:
                            max_lev = max_lev / 100.0

                        taker_fee_bps = None
                        maker_fee_bps = None

                        if isinstance(taker_fee_p, (int, float, str)):
                            try:
                                taker_fee_bps = float(taker_fee_p) / 10000.0
                            except (TypeError, ValueError):
                                pass
                        if isinstance(maker_fee_p, (int, float, str)):
                            try:
                                maker_fee_bps = float(maker_fee_p) / 10000.0
                            except (TypeError, ValueError):
                                pass

                        price_impact_k = pair.get('priceImpactK')
                        if price_impact_k is not None:
                            try:
                                price_impact_k = int(price_impact_k)
                            except (TypeError, ValueError):
                                price_impact_k = None

                        decay_rate = pair.get('decayRate')
                        if decay_rate is not None:
                            try:
                                decay_rate = int(decay_rate)
                            except (TypeError, ValueError):
                                decay_rate = None

                        buy_volume = pair.get('buyVolume')
                        try:
                            buy_volume = int(buy_volume) if buy_volume is not None else 0
                        except (TypeError, ValueError):
                            buy_volume = 0

                        sell_volume = pair.get('sellVolume')
                        try:
                            sell_volume = int(sell_volume) if sell_volume is not None else 0
                        except (TypeError, ValueError):
                            sell_volume = 0

                        last_update = pair.get('lastUpdateTimestamp')
                        if last_update is not None:
                            try:
                                last_update = int(last_update)
                            except (TypeError, ValueError):
                                last_update = None

                        # Rollover / margin-fee fields
                        last_rollover_long_pure = 0
                        try:
                            raw_lrlp = pair.get('lastRolloverLongPure')
                            if raw_lrlp is not None:
                                last_rollover_long_pure = int(raw_lrlp)
                        except (TypeError, ValueError):
                            pass

                        is_negative_rollover_allowed = bool(
                            pair.get('isNegativeRolloverAllowed', False)
                        )

                        last_rollover_block = 0
                        try:
                            rlb = pair.get('lastRolloverBlock')
                            if rlb is not None:
                                last_rollover_block = int(rlb)
                        except (TypeError, ValueError):
                            pass

                        last_funding_time = 0
                        try:
                            lft = pair.get('lastFundingTime')
                            if lft is not None:
                                last_funding_time = int(lft)
                        except (TypeError, ValueError):
                            pass

                        if taker_fee_bps is not None:
                            cache[symbol] = {
                                'fee_bps':                      taker_fee_bps,
                                'maker_fee_bps':                maker_fee_bps if maker_fee_bps is not None else 0.0,
                                'max_leverage':                 float(max_lev) if max_lev is not None else None,
                                'price_impact_k':               price_impact_k,
                                'decay_rate':                   decay_rate,
                                'buy_volume':                   buy_volume,
                                'sell_volume':                  sell_volume,
                                'last_update_timestamp':        last_update,
                                # rollover / margin-fee fields
                                'rollover_fee_per_block':       int(pair.get('rolloverFeePerBlock', 0) or 0),
                                'last_rollover_long_pure':      last_rollover_long_pure,
                                'is_negative_rollover_allowed': is_negative_rollover_allowed,
                                'last_rollover_block':          last_rollover_block,
                                'last_funding_time':            last_funding_time,
                            }
                    # blocks_per_day is fetched live via Arbitrum RPC in get_rollover_rate_24h

        except Exception:
            log.exception("Error loading Ostium metadata from pairs API")

        # 2. Load Seasons (Fee Overrides)
        try:
            seasons_url = "https://onlypoints.ostium.io/api/seasons/current"
            headers = {'Accept': 'application/json', 'Referer': 'https://app.ostium.io/'}
            response = self.session.get(seasons_url, headers=headers, timeout=30)
            if response.status_code == 200:
                s_data  = response.json()
                season  = s_data.get('season', {})
                mode    = season.get('mode', {})
                assets  = mode.get('assets', [])
                if isinstance(assets, list):
                    for asset_item in assets:
                        a_id    = asset_item.get('assetId')
                        new_fee = asset_item.get('newFee')
                        if a_id is not None and new_fee is not None and a_id in pair_id_map:
                            symbol = pair_id_map[a_id]
                            if symbol in cache:
                                new_fee_bps = float(new_fee) * 100.0
                                cache[symbol]['fee_bps']       = new_fee_bps
                                cache[symbol]['maker_fee_bps'] = new_fee_bps
        except Exception:
            log.exception("Error loading Ostium seasons data")

        return cache

    def get_fee_bps(self, ostium_symbol: str) -> Optional[float]:
        """Get the opening fee for an Ostium asset. Returns None if not available."""
        data = self.metadata_cache.get(ostium_symbol)
        return data.get('fee_bps') if data else None

    def get_maker_fee_bps(self, ostium_symbol: str) -> Optional[float]:
        """Get the maker fee for an Ostium asset. Returns None if not available."""
        data = self.metadata_cache.get(ostium_symbol)
        return data.get('maker_fee_bps') if data else None

    def get_max_leverage(self, ostium_symbol: str) -> Optional[float]:
        """Get max leverage."""
        data = self.metadata_cache.get(ostium_symbol)
        return data.get('max_leverage') if data else None

    # ------------------------------------------------------------------
    # Rollover / margin fee  (confirmed formula via lastRolloverLongPure)
    # ------------------------------------------------------------------
    def _fetch_blocks_per_day(self) -> float:
        """
        Fetch live Arbitrum block number via public RPC and compute blocks/day
        from (currentBlock - lastRolloverBlock) / (currentTime - lastFundingTime) × 86400
        using the most recently updated pair as the reference point.
        Falls back to 345_600 (~4 blk/s) if unavailable.
        """
        now = time.time()
        if self._blocks_per_day and (now - self._blocks_per_day_fetched) < self._blocks_per_day_ttl:
            return self._blocks_per_day

        try:
            payload    = {'jsonrpc': '2.0', 'method': 'eth_blockNumber', 'params': [], 'id': 1}
            resp       = requests.post(self.ARB_RPC, json=payload, timeout=10)
            curr_block = int(resp.json().get('result', '0x0'), 16)

            candidates = [
                (v['last_rollover_block'], v['last_funding_time'])
                for v in self.metadata_cache.values()
                if v.get('last_rollover_block', 0) > 0 and v.get('last_funding_time', 0) > 0
            ]
            candidates.sort(reverse=True)

            if candidates and curr_block > candidates[0][0]:
                ref_block, ref_time = candidates[0]
                db = curr_block - ref_block
                dt = now - ref_time
                if dt > 0 and db > 0:
                    self._blocks_per_day        = (db / dt) * 86_400
                    self._blocks_per_day_fetched = now
                    return self._blocks_per_day
        except Exception:
            log.exception("Ostium RPC error")

        self._blocks_per_day = 345_600  # fallback ~4 blk/s
        return self._blocks_per_day

    # ------------------------------------------------------------------
    # Dynamic spread helpers  (Solidity-compatible Pade approximation)
    # ------------------------------------------------------------------
    def _decay_volume_with_pade(self, volume: int, decay_interval: int, decay_rate: int) -> int:
        """Decay volume using Pade approximation (mirrors Solidity _decayVolumeWithPade)."""
        if decay_interval == 0 or decay_rate == 0:
            return volume

        decay_factor_half = decay_rate * decay_interval // 2
        numerator   = max(0, self.PRECISION_18 - decay_factor_half)
        denominator = self.PRECISION_18 + decay_factor_half

        if denominator == 0:
            return 0

        decay_multiplier = numerator * self.PRECISION_18 // denominator
        return volume * decay_multiplier // self.PRECISION_18

    def _get_decayed_volumes_usd(self, asset_data: Dict) -> Tuple[float, float]:
        """
        Get decayed buy and sell volumes in USD.
        Returns: (decayed_buy_volume_usd, decayed_sell_volume_usd)
        """
        decay_rate   = asset_data.get('decay_rate') or 0
        buy_volume   = asset_data.get('buy_volume') or 0
        sell_volume  = asset_data.get('sell_volume') or 0
        last_update  = asset_data.get('last_update_timestamp') or int(time.time())

        current_time = int(time.time())
        dt = max(0, current_time - last_update)

        decayed_buy  = self._decay_volume_with_pade(buy_volume,  dt, decay_rate)
        decayed_sell = self._decay_volume_with_pade(sell_volume, dt, decay_rate)

        # Volumes are stored as collateral * leverage * PRECISION_10 (leverage *100)
        decayed_buy_usd  = decayed_buy  / (100 * self.PRECISION_10)
        decayed_sell_usd = decayed_sell / (100 * self.PRECISION_10)

        return decayed_buy_usd, decayed_sell_usd

    def _calculate_dynamic_spread(
        self,
        notional_usd: float,
        price_impact_k: int,
        mid_price: float,
        ask_price: float,
        bid_price: float,
        initial_volume_usd: float = 0.0,
    ) -> float:
        """
        Calculate spread using formula that matches Ostium UI.

        Formula: spread_bps = market_spread/2 + (initialVolume + tradeSize/2) * priceImpactK / 1e27 * 10000

        Returns: Spread in basis points.
        """
        ba_spread_bps      = (ask_price - bid_price) / mid_price * 10000
        market_spread_half = ba_spread_bps / 2

        avg_volume          = initial_volume_usd + notional_usd / 2
        dynamic_spread_bps  = avg_volume * price_impact_k / self.PRECISION_27 * 10000

        return market_spread_half + dynamic_spread_bps

    # ------------------------------------------------------------------
    # Orderbook (oracle-based synthetic)
    # ------------------------------------------------------------------
    def get_latest_price(self, asset: str, max_retries: int = 5) -> Optional[Dict]:
        """Get the latest price for a specific asset with retry logic."""
        url    = f"{self.BASE_URL}/PricePublish/latest-price"
        params = {"asset": asset}

        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    if data and data.get('mid', 0) > 0:
                        return data
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    log.warning("Ostium price fetch failed for %s after %d attempts", asset, max_retries)
        return None

    def get_orderbook(self, symbol: str) -> Optional[Dict]:
        """Fetch price and return as synthetic orderbook (Ostium is oracle-based)."""
        price_data = self.get_latest_price(symbol)
        return price_data if price_data else None

    def normalize_orderbook(self, orderbook: Dict, depth_usd: float) -> Optional[StandardizedOrderbook]:
        """
        Normalize Ostium price data to StandardizedOrderbook.
        Uses depth_usd to simulate available liquidity at the oracle price.
        """
        if not orderbook:
            return None

        bid = float(orderbook.get('bid', 0))
        ask = float(orderbook.get('ask', 0))
        mid = float(orderbook.get('mid', 0))

        if bid <= 0 or ask <= 0:
            return None

        std_bids = [{'price': bid, 'qty': depth_usd / bid}]
        std_asks = [{'price': ask, 'qty': depth_usd / ask}]

        return StandardizedOrderbook(
            bids=std_bids,
            asks=std_asks,
            best_bid=bid,
            best_ask=ask,
            mid_price=mid,
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(self, asset: str, order_size_usd: float) -> Optional[Dict]:
        """
        Calculate execution cost with dynamic spread.

        Uses Ostium's dynamic spread formula when priceImpactK is available;
        falls back to basic bid/ask spread otherwise.
        """
        raw_data = self.get_orderbook(asset)
        if not raw_data:
            return None

        mid_price = float(raw_data.get('mid', 0))
        bid_price = float(raw_data.get('bid', 0))
        ask_price = float(raw_data.get('ask', 0))

        if mid_price <= 0 or bid_price <= 0 or ask_price <= 0:
            return None

        asset_data = self.metadata_cache.get(asset)
        if not asset_data:
            return None

        open_fee_bps  = asset_data.get('fee_bps')
        maker_fee_bps = asset_data.get('maker_fee_bps', 0.0)

        if open_fee_bps is None:
            return None

        price_impact_k = asset_data.get('price_impact_k')
        is_dynamic     = price_impact_k is not None and price_impact_k > 0

        ba_spread_bps   = (ask_price - bid_price) / mid_price * 10000
        basic_spread_half = ba_spread_bps / 2

        if is_dynamic:
            decayed_buy_usd, decayed_sell_usd = self._get_decayed_volumes_usd(asset_data)

            buy_spread_bps = self._calculate_dynamic_spread(
                notional_usd=order_size_usd,
                price_impact_k=price_impact_k,
                mid_price=mid_price,
                ask_price=ask_price,
                bid_price=bid_price,
                initial_volume_usd=decayed_buy_usd,
            )
            sell_spread_bps = self._calculate_dynamic_spread(
                notional_usd=order_size_usd,
                price_impact_k=price_impact_k,
                mid_price=mid_price,
                ask_price=ask_price,
                bid_price=bid_price,
                initial_volume_usd=decayed_sell_usd,
            )
            avg_spread_bps = (buy_spread_bps + sell_spread_bps) / 2
        else:
            buy_spread_bps  = basic_spread_half
            sell_spread_bps = basic_spread_half
            avg_spread_bps  = basic_spread_half

        buy_exec_price  = mid_price * (1 + buy_spread_bps  / 10000)
        sell_exec_price = mid_price * (1 - sell_spread_bps / 10000)

        return {
            'mid_price':               mid_price,
            'best_bid':                bid_price,
            'best_ask':                ask_price,
            'slippage_bps':            avg_spread_bps,
            'buy_slippage_bps':        buy_spread_bps,
            'sell_slippage_bps':       sell_spread_bps,
            'fee_bps':                 open_fee_bps,
            'maker_fee_bps':           maker_fee_bps,
            'is_market_open':          raw_data.get('isMarketOpen', False),
            'max_leverage':            asset_data.get('max_leverage'),
            'is_dynamic_spread':       is_dynamic,
            'timestamp':               time.time(),
            'buy':  {'avg_price': buy_exec_price,  'slippage_bps': buy_spread_bps,  'levels_used': 1},
            'sell': {'avg_price': sell_exec_price, 'slippage_bps': sell_spread_bps, 'levels_used': 1},
        }

