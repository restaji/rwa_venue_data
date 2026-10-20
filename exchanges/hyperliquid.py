#!/usr/bin/env python3
"""
exchanges/hyperliquid.py - Hyperliquid exchange integration.

Covers: orderbook (L2 book with precision cascade), fees (growth-mode aware),
and max leverage – all fetched dynamically from public APIs (no auth required).
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

from models import (
    HYPERLIQUID_GROWTH_MODE_SCALE,
    HYPERLIQUID_NO_GROWTH_MODE_SCALE,
    StandardizedOrderbook,
    ExecutionCalculator,
    floor_leverage,
)


class HyperliquidAPI:
    def __init__(self):
        self.base_url = "https://api.hyperliquid.xyz/info"
        self.headers  = {'Content-Type': 'application/json'}

        self.max_leverages_cache: Dict = {}
        self.growth_mode_cache:   Dict = {}   
        self.fee_cache:           Dict = {}   
        self.funding_cache:       Dict = {}   
        self.deployer_fee_scale_cache: Dict = {}   # per-asset, from meta universe

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
    ZERO_ADDRESS = "0x0000000000000000000000000000000000000001"

    def _post_info(self, payload: Dict):
        response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
        if response.status_code != 200:
            return None
        return response.json()

    def _apply_perpdex_scales(self, dexes) -> None:
        for dex in dexes or []:
            if not dex:
                continue
            scale = dex.get("deployerFeeScale")
            if scale is None:
                continue
            name = dex.get("name")
            if name == "xyz":
                self.deployer_fee_scale = float(scale)
            elif name == "flx":
                self.flx_deployer_fee_scale = float(scale)

    def _apply_user_fees(self, dex: str) -> None:
        fees = self._post_info({
            "type": "userFees",
            "user": self.ZERO_ADDRESS,
            "dex": dex,
        })
        if not isinstance(fees, dict):
            return
        taker = fees.get("userCrossRate")
        maker = fees.get("userAddRate")
        if dex == "flx":
            if taker is not None:
                self.flx_base_taker_rate = float(taker)
            if maker is not None:
                self.flx_base_maker_rate = float(maker)
            return
        if taker is not None:
            self.base_taker_rate = float(taker)
        if maker is not None:
            self.base_maker_rate = float(maker)

    def _fetch_fee_config(self):
        """Fetch fee configuration from public APIs (no auth required)."""
        if time.time() - self.last_fee_fetch < self.fee_cache_ttl and self.deployer_fee_scale is not None:
            return
        try:
            self._apply_perpdex_scales(self._post_info({"type": "perpDexs"}))
            self._apply_user_fees("xyz")
            self._apply_user_fees("flx")
            self.last_fee_fetch = time.time()
        except (TypeError, ValueError, KeyError, OSError):
            log.exception("Error fetching HL fee config")

    @staticmethod
    def _parse_meta_payload(data) -> Tuple[list, list]:
        if isinstance(data, list) and data and isinstance(data[0], dict):
            universe   = data[0].get("universe", [])
            asset_ctxs = data[1] if len(data) > 1 else []
            return universe, asset_ctxs
        if isinstance(data, dict):
            return data.get("universe", []), []
        return [], []

    def _store_asset_meta(self, dex_name: str, item: Dict, funding_1h: float) -> None:
        name = item.get("name")
        if not name:
            return
        try:
            fee_scale = float(item["deployerFeeScale"])
        except (KeyError, TypeError, ValueError):
            fee_scale = None
        stripped = name.replace(f"{dex_name}:", "") if name.startswith(f"{dex_name}:") else name
        prefixed = f"{dex_name}:{name}" if not name.startswith(f"{dex_name}:") else name
        for key in (name, stripped, prefixed):
            self.max_leverages_cache[key] = item.get("maxLeverage")
            self.growth_mode_cache[key]   = item.get("growthMode") == "enabled"
            self.funding_cache[key]       = funding_1h
            if fee_scale is not None:
                self.deployer_fee_scale_cache[key] = fee_scale

    def _ingest_dex_metadata(self, dex_name: str) -> None:
        payload  = {"type": "metaAndAssetCtxs", "dex": dex_name}
        response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
        if response.status_code != 200:
            return
        universe, asset_ctxs = self._parse_meta_payload(response.json())
        for i, item in enumerate(universe):
            funding_1h = float(asset_ctxs[i].get("funding", 0)) if i < len(asset_ctxs) else 0.0
            self._store_asset_meta(dex_name, item, funding_1h)

    def _fetch_metadata(self):
        """Fetch metadata to get max leverage and growth mode info (xyz and flx)."""
        if time.time() - self.last_metadata_fetch < self.metadata_cache_ttl and self.max_leverages_cache:
            return
        try:
            self.max_leverages_cache = {}
            self.growth_mode_cache   = {}
            self.funding_cache       = {}
            self.deployer_fee_scale_cache = {}
            for dex_name in ("xyz", "flx"):
                try:
                    self._ingest_dex_metadata(dex_name)
                except (TypeError, ValueError, KeyError, AttributeError, OSError):
                    log.exception("Error ingesting HL metadata for %s", dex_name)
            if self.max_leverages_cache:
                self.last_metadata_fetch = time.time()
        except (TypeError, ValueError, KeyError, AttributeError, OSError):
            log.exception("Error fetching HL metadata")

    def _fee_symbol_keys(self, symbol: str) -> Tuple[bool, str, str]:
        if self._is_flx_symbol(symbol):
            search = self._resolve_flx_coin(symbol)  # e.g. flx:OIL-USDH -> flx:OIL
            return True, search, search.replace("flx:", "")
        search = symbol if symbol.startswith("xyz:") else f"xyz:{symbol}"
        plain  = symbol.replace("xyz:", "") if symbol.startswith("xyz:") else symbol
        return False, search, plain

    def _fee_base_rates(self, is_flx: bool, search_symbol: str, plain_symbol: str):
        growth_enabled = self.growth_mode_cache.get(
            search_symbol, self.growth_mode_cache.get(plain_symbol, True),
        )
        # deployerFeeScale is published per-asset in the meta universe. The
        # dex-level value from perpDexs is only a fallback — it was dropped from
        # that response, so it is usually None now.
        asset_scale = self.deployer_fee_scale_cache.get(
            search_symbol, self.deployer_fee_scale_cache.get(plain_symbol),
        )
        if is_flx:
            scale = asset_scale if asset_scale is not None else self.flx_deployer_fee_scale
            return growth_enabled, scale, self.flx_base_taker_rate, self.flx_base_maker_rate
        scale = asset_scale if asset_scale is not None else self.deployer_fee_scale
        return growth_enabled, scale, self.base_taker_rate, self.base_maker_rate

    def _calculate_fees_for_asset(self, symbol: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Calculate taker and maker fees using the official Hyperliquid formula.
        Returns (None, None) if fee config has not yet been loaded from the API.
        """
        self._fetch_fee_config()
        self._fetch_metadata()

        is_flx, search_symbol, plain_symbol = self._fee_symbol_keys(symbol)
        if search_symbol in self.fee_cache:
            return self.fee_cache[search_symbol]

        growth_enabled, deployer_fee_scale, base_taker, base_maker = self._fee_base_rates(
            is_flx, search_symbol, plain_symbol,
        )
        if base_taker is None or base_maker is None or deployer_fee_scale is None:
            return (None, None)

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
    def get_max_leverage(self, symbol: str) -> Optional[int]:
        self._fetch_metadata()
        if self._is_flx_symbol(symbol):
            key = self._resolve_flx_coin(symbol)  # flx:OIL-USDH -> flx:OIL
            return floor_leverage(self.max_leverages_cache.get(key))
        return floor_leverage(self.max_leverages_cache.get(f"xyz:{symbol}"))

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

    # l2Book is the only per-request call in the compare path, so a 429 here
    # silently drops the whole venue from the comparison. Retry briefly.
    _BOOK_RETRIES     = 3
    _BOOK_RETRY_DELAY = 0.4

    @staticmethod
    def _normalize_level(lvl):
        if isinstance(lvl, dict):
            return lvl
        if isinstance(lvl, list) and len(lvl) >= 2:
            return {'px': str(lvl[0]), 'sz': str(lvl[1])}
        return None

    def _parse_l2_book(self, response) -> Optional[Dict]:
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
        formatted_bids = [x for x in (self._normalize_level(b) for b in bids) if x]
        formatted_asks = [x for x in (self._normalize_level(a) for a in asks) if x]
        return {'levels': [formatted_bids, formatted_asks]}

    def _fetch_coin(self, coin: str, n_sig_figs: Optional[int]) -> Optional[Dict]:
        payload = {"type": "l2Book", "coin": coin}
        if n_sig_figs is not None:
            payload["nSigFigs"] = n_sig_figs

        try:
            response = None
            for attempt in range(self._BOOK_RETRIES):
                response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    break
                if response.status_code == 429 and attempt < self._BOOK_RETRIES - 1:
                    time.sleep(self._BOOK_RETRY_DELAY * (attempt + 1))
                    continue
                log.warning("HL l2Book %s failed: HTTP %s %s",
                            coin, response.status_code, response.text[:120])
                return None
            return self._parse_l2_book(response)
        except (TypeError, ValueError, KeyError, AttributeError, OSError):
            log.exception("HL l2Book error for %s", coin)
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

    @staticmethod
    def _true_mid_from_raw(raw_default) -> Optional[float]:
        if not raw_default:
            return None
        try:
            lvl = raw_default.get('levels', [[], []])
            bids_def, asks_def = lvl[0], lvl[1]
            if not bids_def:
                return None
            if not asks_def:
                return None
            return (float(bids_def[0]['px']) + float(asks_def[0]['px'])) / 2
        except (TypeError, ValueError, KeyError, AttributeError, OSError):
            return None

    def _hl_display_symbol(self, symbol: str) -> str:
        if self._is_flx_symbol(symbol):
            return symbol if symbol.startswith("flx:") else f"flx:{symbol}"
        return symbol if 'xyz' in str(symbol) else f'xyz:{symbol}'

    def get_optimal_execution(self, symbol: str, order_size_usd: float) -> Optional[Dict]:
        """
        Flow:
          1. Fetch the default (max) precision orderbook and pin the true mid.
          2. Fill the order against that book.
             → If depth is insufficient the result stays PARTIAL — never hidden.
        """
        taker_fee_bps, maker_fee_bps = self.get_fees(symbol)
        if taker_fee_bps is None:
            return None

        raw_default = self.get_orderbook(symbol, n_sig_figs=None)
        true_mid = self._true_mid_from_raw(raw_default)
        std_default = self.normalize_orderbook(raw_default) if raw_default else None
        if not std_default:
            return None

        result = ExecutionCalculator.calculate_execution_cost(
            std_default, order_size_usd, open_fee_bps=taker_fee_bps
        )
        if not result:
            return None

        result['fee_bps']       = taker_fee_bps
        result['maker_fee_bps'] = maker_fee_bps
        result['sig_figs']      = 'Maximum'
        result['is_xyz']        = not self._is_flx_symbol(symbol)
        result['symbol']        = self._hl_display_symbol(symbol)
        result['max_leverage']  = self.get_max_leverage(symbol)
        result['true_mid']      = true_mid
        return result
