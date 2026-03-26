#!/usr/bin/env python3
"""
exchanges/hyperliquid.py - Hyperliquid exchange integration.

Covers: orderbook (L2 book with precision cascade), fees (growth-mode aware),
and max leverage – all fetched dynamically from public APIs (no auth required).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

from models import (
    HYPERLIQUID_GROWTH_MODE_SCALE,
    HYPERLIQUID_NO_GROWTH_MODE_SCALE,
    StandardizedOrderbook,
    ExecutionCalculator,
)


class HyperliquidAPI:
    def __init__(self):
        self.base_url = "https://api.hyperliquid.xyz/info"
        self.headers  = {'Content-Type': 'application/json'}

        self.max_leverages_cache: Dict = {}
        self.growth_mode_cache:   Dict = {}   
        self.fee_cache:           Dict = {}   
        self.funding_cache:       Dict = {}   

        self.deployer_fee_scale: Optional[float] = None
        self.base_taker_rate:    Optional[float] = None
        self.base_maker_rate:    Optional[float] = None

        self.flx_deployer_fee_scale: Optional[float] = None
        self.flx_base_taker_rate:    Optional[float] = None
        self.flx_base_maker_rate:    Optional[float] = None

        self.last_metadata_fetch = 0
        self.last_fee_fetch      = 0
        self.metadata_cache_ttl  = 300   # 5 min
        self.fee_cache_ttl       = 300

        # xyz dex uses different names for some RWA assets
        self._symbol_aliases: Dict[str, str] = {
            "XAU": "GOLD", "XAG": "SILVER",
            "EURUSD": "EUR", "USDJPY": "JPY",
        }

    def _is_flx_symbol(self, symbol: str) -> bool:
        """True if this symbol is on the flx dex (e.g. flx:OIL-USDH), not xyz."""
        return symbol.startswith("flx:")

    def _resolve_flx_coin(self, symbol: str) -> str:
        """Ensure symbol is in flx:COIN format for API calls."""
        plain = symbol.replace("flx:", "") if symbol.startswith("flx:") else symbol
        return f"flx:{plain}"

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------
    def _fetch_fee_config(self):
        """Fetch fee configuration from public APIs (no auth required)."""
        if time.time() - self.last_fee_fetch < self.fee_cache_ttl and self.deployer_fee_scale is not None:
            return

        try:
            # 1. deployerFeeScale from perpDexs API
            payload  = {"type": "perpDexs"}
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code == 200:
                dexes = response.json()
                for dex in dexes:
                    if dex and dex.get("name") == "xyz":
                        v = dex.get("deployerFeeScale")
                        if v is not None:
                            self.deployer_fee_scale = float(v)
                    elif dex and dex.get("name") == "flx":
                        v = dex.get("deployerFeeScale")
                        if v is not None:
                            self.flx_deployer_fee_scale = float(v)

            # 2. Base fee rates from userFees API (public zero-address)
            payload  = {"type": "userFees", "user": "0x0000000000000000000000000000000000000001", "dex": "xyz"}
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code == 200:
                fees = response.json()
                taker = fees.get("userCrossRate")
                maker = fees.get("userAddRate")
                if taker is not None:
                    self.base_taker_rate = float(taker)
                if maker is not None:
                    self.base_maker_rate = float(maker)

            payload  = {"type": "userFees", "user": "0x0000000000000000000000000000000000000001", "dex": "flx"}
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code == 200:
                fees = response.json()
                taker = fees.get("userCrossRate")
                maker = fees.get("userAddRate")
                if taker is not None:
                    self.flx_base_taker_rate = float(taker)
                if maker is not None:
                    self.flx_base_maker_rate = float(maker)

            self.last_fee_fetch = time.time()
        except Exception:
            log.exception("Error fetching HL fee config")

    def _fetch_metadata(self):
        """Fetch metadata to get max leverage and growth mode info (xyz and flx)."""
        if time.time() - self.last_metadata_fetch < self.metadata_cache_ttl and self.max_leverages_cache:
            return

        try:
            self.max_leverages_cache = {}
            self.growth_mode_cache   = {}
            self.funding_cache       = {}

            for dex_name in ("xyz", "flx"):
                payload  = {"type": "metaAndAssetCtxs", "dex": dex_name}
                response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
                if response.status_code != 200:
                    continue
                data     = response.json()
                universe = []
                asset_ctxs = []
                if isinstance(data, list) and len(data) >= 1:
                    universe   = data[0].get("universe", [])
                    asset_ctxs = data[1] if len(data) > 1 else []
                elif isinstance(data, dict):
                    universe = data.get("universe", [])

                for i, item in enumerate(universe):
                    name        = item.get("name")
                    max_lev     = item.get("maxLeverage")
                    growth_mode = item.get("growthMode")
                    funding_1h  = float(asset_ctxs[i].get("funding", 0)) if i < len(asset_ctxs) else 0.0

                    if name:
                        stripped = name.replace(f"{dex_name}:", "") if name.startswith(f"{dex_name}:") else name
                        prefixed = f"{dex_name}:{name}" if not name.startswith(f"{dex_name}:") else name
                        for key in (name, stripped, prefixed):
                            self.max_leverages_cache[key] = max_lev
                            self.growth_mode_cache[key]   = growth_mode == "enabled"
                            self.funding_cache[key]       = funding_1h

            self.last_metadata_fetch = time.time()
        except Exception:
            log.exception("Error fetching HL metadata")

    def _calculate_fees_for_asset(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Calculate taker and maker fees using the official Hyperliquid formula.
        Returns (None, None) if fee config has not yet been loaded from the API.
        """
        self._fetch_fee_config()
        self._fetch_metadata()

        is_flx = self._is_flx_symbol(symbol)
        if is_flx:
            search_symbol = self._resolve_flx_coin(symbol)  # e.g. flx:OIL-USDH -> flx:OIL
            plain_symbol  = search_symbol.replace("flx:", "")
        else:
            search_symbol = symbol if symbol.startswith("xyz:") else f"xyz:{symbol}"
            plain_symbol  = symbol.replace("xyz:", "") if symbol.startswith("xyz:") else symbol

        if search_symbol in self.fee_cache:
            return self.fee_cache[search_symbol]

        growth_enabled   = self.growth_mode_cache.get(search_symbol,
                           self.growth_mode_cache.get(plain_symbol, True))

        if is_flx:
            if self.flx_base_taker_rate is None or self.flx_deployer_fee_scale is None:
                return (None, None)
            deployer_fee_scale = self.flx_deployer_fee_scale
            base_taker         = self.flx_base_taker_rate
            base_maker         = self.flx_base_maker_rate
        else:
            if self.base_taker_rate is None or self.deployer_fee_scale is None:
                return (None, None)
            deployer_fee_scale = self.deployer_fee_scale
            base_taker         = self.base_taker_rate
            base_maker         = self.base_maker_rate

        scale_if_hip3 = (deployer_fee_scale + 1) if deployer_fee_scale < 1 else (deployer_fee_scale * 2)
        growth_scale  = HYPERLIQUID_GROWTH_MODE_SCALE if growth_enabled else HYPERLIQUID_NO_GROWTH_MODE_SCALE

        taker_fee_bps = base_taker * 100 * scale_if_hip3 * growth_scale * 100
        maker_fee_bps = base_maker * 100 * scale_if_hip3 * growth_scale * 100

        self.fee_cache[search_symbol] = (taker_fee_bps, maker_fee_bps)
        self.fee_cache[plain_symbol]  = (taker_fee_bps, maker_fee_bps)

        return (taker_fee_bps, maker_fee_bps)

    def get_fees(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Get taker and maker fees for a symbol (public API, no auth required).
        Returns: (taker_fee_bps, maker_fee_bps) — both None if fee config not loaded.
        """
        return self._calculate_fees_for_asset(symbol)

    # ------------------------------------------------------------------
    # Max leverage
    # ------------------------------------------------------------------
    def get_max_leverage(self, symbol: str) -> Optional[float]:
        self._fetch_metadata()
        if self._is_flx_symbol(symbol):
            key = self._resolve_flx_coin(symbol)  # flx:OIL-USDH -> flx:OIL
            return self.max_leverages_cache.get(key)
        return self.max_leverages_cache.get(f"xyz:{symbol}")

    def _resolve_xyz_name(self, symbol: str) -> str:
        """Map common RWA ticker to xyz dex internal name."""
        upper = symbol.upper().replace("XYZ:", "")
        return self._symbol_aliases.get(upper, upper)

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def normalize_symbol(self, symbol: str) -> str:
        if symbol.startswith("flx:"):
            return symbol
        s = symbol.upper()
        if s == "NDX":
            return "kPW"
        return s

    def _fetch_coin(self, coin: str, n_sig_figs: Optional[int]) -> Optional[Dict]:
        payload = {"type": "l2Book", "coin": coin}
        if n_sig_figs is not None:
            payload["nSigFigs"] = n_sig_figs

        try:
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
            if response.status_code != 200:
                return None
            data = response.json()
            if not data:
                return None

            levels = data.get('levels', [])
            if not isinstance(levels, list) or len(levels) < 2:
                return None

            bids = levels[0] if isinstance(levels[0], list) else []
            asks = levels[1] if isinstance(levels[1], list) else []
            if not bids or not asks:
                return None

            def normalize_level(lvl):
                if isinstance(lvl, dict):
                    return lvl
                if isinstance(lvl, list) and len(lvl) >= 2:
                    return {'px': str(lvl[0]), 'sz': str(lvl[1])}
                return None

            formatted_bids = [x for x in (normalize_level(b) for b in bids) if x]
            formatted_asks = [x for x in (normalize_level(a) for a in asks) if x]

            return {'levels': [formatted_bids, formatted_asks]}
        except Exception:
            return None

    def get_orderbook(self, symbol: str, n_sig_figs: Optional[int] = None) -> Optional[Dict]:
        """Fetch raw orderbook at the requested precision. No fallback logic here."""
        raw_symbol = self.normalize_symbol(symbol)
        if self._is_flx_symbol(raw_symbol):
            coin = self._resolve_flx_coin(raw_symbol)  # flx:OIL-USDH -> flx:OIL
        else:
            coin = raw_symbol if raw_symbol.startswith("xyz:") else f"xyz:{raw_symbol}"
        return self._fetch_coin(coin, n_sig_figs)

    def normalize_orderbook(self, orderbook: Dict) -> Optional[StandardizedOrderbook]:
        """Normalize Hyperliquid orderbook to StandardizedOrderbook."""
        if not orderbook:
            return None

        levels = orderbook.get('levels', [[], []])
        bids   = levels[0] if len(levels) > 0 else []
        asks   = levels[1] if len(levels) > 1 else []

        if not asks or not bids:
            return None

        try:
            best_bid = orderbook.get('true_best_bid') or float(bids[0].get('px', 0))
            best_ask = orderbook.get('true_best_ask') or float(asks[0].get('px', 0))
        except (ValueError, AttributeError, IndexError):
            return None

        if best_bid <= 0 or best_ask <= 0:
            return None

        def parse(entries, px_key='px', sz_key='sz'):
            result = []
            for e in entries:
                try:
                    result.append({'price': float(e.get(px_key, 0)), 'qty': float(e.get(sz_key, 0))})
                except (ValueError, AttributeError):
                    continue
            return result

        std_bids  = parse(bids)
        std_asks  = parse(asks)
        mid_price = (best_bid + best_ask) / 2

        return StandardizedOrderbook(
            bids=std_bids, asks=std_asks,
            best_bid=best_bid, best_ask=best_ask,
            mid_price=mid_price, timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(
        self,
        orderbook: Dict,
        order_size_usd: float,
        symbol: Optional[str] = None,
        anchor_mid_price: Optional[float] = None,
    ) -> Optional[Dict]:
        """Calculate execution cost using ExecutionCalculator with dynamic fees."""
        std_orderbook = self.normalize_orderbook(orderbook)
        if not std_orderbook:
            return None

        if anchor_mid_price:
            std_orderbook = StandardizedOrderbook(
                bids=std_orderbook.bids, asks=std_orderbook.asks,
                best_bid=std_orderbook.best_bid, best_ask=std_orderbook.best_ask,
                mid_price=anchor_mid_price, timestamp=std_orderbook.timestamp,
            )

        if not symbol:
            return None

        taker_fee_bps, maker_fee_bps = self.get_fees(symbol)

        result = ExecutionCalculator.calculate_execution_cost(
            std_orderbook, order_size_usd,
            open_fee_bps=taker_fee_bps,
            close_fee_bps=0.0,
        )

        if result:
            result['fee_bps']        = taker_fee_bps
            result['maker_fee_bps']  = maker_fee_bps
            max_levels_hit = (
                result['buy']['levels_used']  >= len(std_orderbook.asks) or
                result['sell']['levels_used'] >= len(std_orderbook.bids)
            )
            result['max_levels_hit'] = max_levels_hit

        return result

    def get_optimal_execution(self, symbol: str, order_size_usd: float) -> Optional[Dict]:
        """
        Flow:
          1. Always fetch default (max) precision first → pin true mid price.
          2. Try to fill the order at default precision.
             → If fully filled: done.
          3. If not filled: fallback to nSigFigs=4 (deeper aggregated book),
             BUT keep mid_price anchored from step 1 for accurate slippage.
          4. If still not filled even on nSigFigs=4: report PARTIAL — never hide it.
        """
        taker_fee_bps, maker_fee_bps = self.get_fees(symbol)
        if taker_fee_bps is None:
            return None

        # ── Step 1: Fetch default precision and pin true mid ─────────────────
        raw_default = self.get_orderbook(symbol, n_sig_figs=None)
        true_mid: Optional[float] = None

        if raw_default:
            try:
                lvl = raw_default.get('levels', [[], []])
                bids_def, asks_def = lvl[0], lvl[1]
                if bids_def and asks_def:
                    true_mid = (float(bids_def[0]['px']) + float(asks_def[0]['px'])) / 2
            except Exception:
                pass

        # ── Step 2: Try fill at default precision ────────────────────────────
        final_result = None
        std_default  = self.normalize_orderbook(raw_default) if raw_default else None

        if std_default:
            result = ExecutionCalculator.calculate_execution_cost(
                std_default, order_size_usd, open_fee_bps=taker_fee_bps
            )
            if result:
                final_result = result
                final_result['fee_bps']       = taker_fee_bps
                final_result['maker_fee_bps'] = maker_fee_bps
                final_result['sig_figs']      = 'Maximum'

        # ── Step 3: Fallback to nSigFigs=5 if not yet fully filled ───────────
        if not final_result or not final_result.get('filled'):
            raw_agg = self.get_orderbook(symbol, n_sig_figs=5)
            std_agg = self.normalize_orderbook(raw_agg) if raw_agg else None

            if std_agg:
                # Anchor mid_price to the true default-precision mid
                if true_mid is not None:
                    std_agg = StandardizedOrderbook(
                    bids=std_agg.bids,
                    asks=std_agg.asks,
                        best_bid=std_agg.best_bid,
                        best_ask=std_agg.best_ask,
                        mid_price=true_mid,
                    timestamp=std_agg.timestamp,
                )

                result_agg = ExecutionCalculator.calculate_execution_cost(
                    std_agg, order_size_usd, open_fee_bps=taker_fee_bps
                )
                if result_agg:
                    final_result = result_agg
                    final_result['fee_bps']       = taker_fee_bps
                    final_result['maker_fee_bps'] = maker_fee_bps
                    final_result['sig_figs']      = 5
                    # Step 4: if still not filled → stays as PARTIAL (executed='PARTIAL')

        # ── Attach metadata ──────────────────────────────────────────────────
        if final_result:
            is_flx = self._is_flx_symbol(symbol)
            final_result['is_xyz']       = not is_flx
            if is_flx:
                display_symbol = symbol if symbol.startswith("flx:") else f"flx:{symbol}"
            else:
                display_symbol = symbol if 'xyz' in str(symbol) else f'xyz:{symbol}'
            final_result['symbol']       = display_symbol
            final_result['max_leverage'] = self.get_max_leverage(symbol)
            final_result['true_mid']     = true_mid

        return final_result
