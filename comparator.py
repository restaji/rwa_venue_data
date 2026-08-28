#!/usr/bin/env python3
"""
comparator.py - FeeComparator orchestrator.

Calls all exchange APIs in parallel conceptually and returns a unified
comparison result with total costs and winner determination.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

from models import ASSETS, floor_leverage
from exchanges.hyperliquid  import HyperliquidAPI
from exchanges.lighter      import LighterAPI
from exchanges.aster        import AsterAPI
from exchanges.avantis      import AvantisAPI
from exchanges.ostium       import OstiumAPI
from exchanges.extended     import ExtendedAPI
from exchanges.edgex        import EdgeXAPI
from exchanges.grvt         import GRVTAPI
from exchanges.variational  import VariationalAPI
from exchanges.ondo         import OndoAPI
from exchanges.pacifica     import PacificaAPI


def _finite(val, default: float = 0.0) -> float:
    """Coerce to float; None / NaN / inf / junk → default. Avoids round(None) 500s."""
    if val is None:
        return default
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


ALL_VENUES = (
    'hyperliquid', 'lighter', 'aster', 'avantis', 'ostium', 'extended',
    'edgex', 'grvt', 'variational', 'ondo', 'pacifica',
)
MAKER_SLIP_VENUES = (
    'hyperliquid', 'lighter', 'aster', 'extended', 'edgex', 'grvt', 'ondo', 'pacifica',
)
DIRECTION_SLIP_VENUES = (
    'hyperliquid', 'lighter', 'aster', 'ostium', 'extended', 'edgex',
    'grvt', 'variational', 'ondo', 'pacifica',
)
_FETCH_ERRORS = (TypeError, ValueError, KeyError, AttributeError, OSError, RuntimeError)


class FeeComparator:
    def __init__(self):
        self.hyperliquid  = HyperliquidAPI()
        self.lighter      = LighterAPI()
        self.aster        = AsterAPI()
        self.avantis      = AvantisAPI()
        self.ostium       = OstiumAPI()
        self.extended     = ExtendedAPI()
        self.edgex        = EdgeXAPI()
        self.grvt         = GRVTAPI()
        self.variational  = VariationalAPI()
        self.ondo         = OndoAPI()
        self.pacifica     = PacificaAPI()
        self._warmup_done = threading.Event()
        threading.Thread(target=self._warmup, daemon=True, name="warmup").start()

    _WARMUP_RETRIES = 3
    _WARMUP_DELAY   = 5

    def _retry_preload(self, label: str, ready, load) -> bool:
        for attempt in range(1, self._WARMUP_RETRIES + 1):
            if not ready():
                load()
            if ready():
                return True
            log.warning("%s attempt %d/%d failed", label, attempt, self._WARMUP_RETRIES)
            if attempt < self._WARMUP_RETRIES:
                time.sleep(self._WARMUP_DELAY)
        log.warning("%s unavailable after %d attempts", label, self._WARMUP_RETRIES)
        return False

    def _preload_edgex(self) -> None:
        if self._retry_preload("EdgeX meta", lambda: bool(self.edgex.meta_cache), self.edgex._load_meta_cache):
            log.info("EdgeX: %d contracts loaded", len(self.edgex.meta_cache))

    def _preload_hyperliquid(self) -> None:
        if self._retry_preload(
            "Hyperliquid fee config",
            lambda: self.hyperliquid.base_taker_rate is not None,
            self.hyperliquid._fetch_fee_config,
        ):
            log.info("Hyperliquid: fee config loaded (base taker=%.4f)", self.hyperliquid.base_taker_rate)

    def _preload_lighter(self) -> None:
        if self._retry_preload(
            "Lighter market cache",
            lambda: self.lighter.market_cache_loaded,
            self.lighter._load_market_cache,
        ):
            log.info("Lighter: market cache loaded (%d markets)", len(self.lighter.market_cache))

    def _preload_grvt(self) -> None:
        if self.grvt.is_ready:
            log.info("GRVT: ready (fees loaded)")
            return
        log.info("GRVT: auth still in progress — background thread will complete it")

    def _preload_avantis(self) -> None:
        try:
            self.avantis._fetch_socket_data()
            log.info("Avantis: pair data loaded (%d pairs)", len(self.avantis._pair_data or {}))
        except (TypeError, ValueError, KeyError, OSError):
            log.exception("Avantis pair data load error")

    def _preload_ondo(self) -> None:
        try:
            self.ondo._load_markets()
            log.info("Ondo: markets loaded (%d markets)", len(self.ondo._markets_cache or {}))
        except (TypeError, ValueError, KeyError, OSError):
            log.exception("Ondo markets load error")

    def _preload_pacifica(self) -> None:
        try:
            self.pacifica._load_meta()
            self.pacifica._load_fees()
            log.info(
                "Pacifica: %d markets loaded (taker=%.2f bps)",
                len(self.pacifica._meta_cache or {}),
                (self.pacifica._fees[0] or 0),
            )
        except (TypeError, ValueError, KeyError, OSError):
            log.exception("Pacifica meta/fees load error")

    def _warm_fees(self, asset_key: str) -> str:
        config = ASSETS[asset_key]
        if config.aster_symbol:
            self.aster.get_fees(config.aster_symbol)
        if config.hyperliquid_symbol:
            self.hyperliquid.get_fees(config.hyperliquid_symbol)
        return asset_key

    @staticmethod
    def _drain_pool(tasks, max_workers: int) -> None:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for fut in as_completed([pool.submit(fn) for fn in tasks]):
                try:
                    fut.result()
                except (TypeError, ValueError, KeyError, OSError):
                    log.exception("Background task error")

    def _warmup(self) -> None:
        """Pre-fetch all exchange caches in the background so startup is instant."""
        assets = list(ASSETS.keys())
        global_tasks = [
            self._preload_hyperliquid, self._preload_edgex, self._preload_lighter,
            self._preload_grvt, self._preload_avantis, self._preload_ondo, self._preload_pacifica,
        ]
        per_asset_tasks = [lambda a=a: self._warm_fees(a) for a in assets]
        all_tasks = global_tasks + per_asset_tasks
        log.info("Warmup: loading caches for %d exchanges + %d assets in parallel …", len(global_tasks), len(assets))
        self._drain_pool(all_tasks, max_workers=min(len(all_tasks), 12))
        self._warmup_done.set()
        log.info("Warmup complete — all caches loaded.")

    # ------------------------------------------------------------------
    # Per-exchange fetchers (kept out of compare_asset so Sonar CC stays low)
    # Each returns (exchange_name, payload, extra) — extra is a live symbol
    # string, a GRVT debug dict, or None.
    # ------------------------------------------------------------------
    @staticmethod
    def _blank_result(config, order_size_usd: float,
                      order_type: str, direction: str, variational_symbol) -> Dict:
        return {
            'asset':          config.name,
            'symbol_key':     config.symbol_key,
            'order_size_usd': order_size_usd,
            'order_type':     order_type,
            'direction':      direction,
            'hyperliquid':    None,
            'lighter':        None,
            'aster':          None,
            'avantis':        None,
            'ostium':         None,
            'extended':       None,
            'edgex':          None,
            'grvt':           None,
            'variational':    None,
            'ondo':           None,
            'pacifica':       None,
            'symbols': {
                'hyperliquid':  config.hyperliquid_symbol,
                'lighter':      config.symbol_key if config.lighter_market_id else None,
                'aster':        config.aster_symbol,
                'avantis':      config.symbol_key,
                'ostium':       config.ostium_symbol,
                'extended':     config.extended_symbol,
                'edgex':        (config.edgex_symbol or config.symbol_key) if config.edgex_contract_id else None,
                'grvt':         config.grvt_instrument,
                'variational':  variational_symbol,
                'ondo':         config.ondo_symbol,
                'pacifica':     config.pacifica_symbol,
            },
        }

    def _fetch_hyperliquid(self, config, order_size_usd: float) -> Tuple[str, Optional[Dict], Any]:
        if not config.hyperliquid_symbol:
            return 'hyperliquid', None, None
        try:
            r = self.hyperliquid.get_optimal_execution(config.hyperliquid_symbol, order_size_usd)
            sym = (r.get('symbol') or config.hyperliquid_symbol) if r else None
            return 'hyperliquid', r, sym
        except _FETCH_ERRORS:
            log.exception("Hyperliquid fetch error")
            return 'hyperliquid', None, None

    def _fetch_lighter(self, config, order_size_usd: float) -> Tuple[str, Optional[Dict], Any]:
        if not config.lighter_market_id:
            return 'lighter', None, None
        try:
            ob = self.lighter.get_orderbook(config.lighter_market_id)
            r  = self.lighter.calculate_execution_cost(ob, order_size_usd, market_id=config.lighter_market_id)
            mk = self.lighter._market_key(config.lighter_market_id)
            live_sym = (self.lighter.market_cache.get(mk) or {}).get("symbol") if mk is not None else None
            if r:
                r['symbol'] = live_sym or config.symbol_key
            return 'lighter', r, live_sym
        except _FETCH_ERRORS:
            log.exception("Lighter fetch error")
            return 'lighter', None, None

    def _fetch_aster(self, config, order_size_usd: float) -> Tuple[str, Optional[Dict], Any]:
        if not config.aster_symbol:
            return 'aster', None, None
        try:
            ob = self.aster.get_orderbook(config.aster_symbol)
            r  = self.aster.calculate_execution_cost(ob, order_size_usd, symbol=config.aster_symbol)
            if r:
                r['symbol'] = config.aster_symbol
            return 'aster', r, None
        except _FETCH_ERRORS:
            log.exception("Aster fetch error")
            return 'aster', None, None

    def _fetch_avantis(self, config, asset_key: str, order_size_usd: float, is_long: bool) -> Tuple[str, Optional[Dict], Any]:
        try:
            r = self.avantis.calculate_cost(asset_key, order_size_usd, is_long=is_long)
            sym = None
            if r:
                # Avantis returns its own ticker (e.g. US500/USD for pair 78);
                # fall back to the registry symbol only if the API omitted it.
                r['symbol'] = r.get('symbol') or config.symbol_key
                sym = r['symbol']
            return 'avantis', r, sym
        except _FETCH_ERRORS:
            log.exception("Avantis fetch error")
            return 'avantis', None, None

    def _fetch_ostium(self, config, order_size_usd: float, is_long: bool) -> Tuple[str, Optional[Dict], Any]:
        if not config.ostium_symbol:
            return 'ostium', None, None
        try:
            r = self.ostium.calculate_execution_cost(config.ostium_symbol, order_size_usd, is_long=is_long)
            if r:
                r['symbol'] = config.ostium_symbol
            return 'ostium', r, None
        except _FETCH_ERRORS:
            log.exception("Ostium fetch error")
            return 'ostium', None, None

    def _fetch_extended(self, config, order_size_usd: float) -> Tuple[str, Optional[Dict], Any]:
        if not config.extended_symbol:
            return 'extended', None, None
        try:
            ob = self.extended.get_orderbook(config.extended_symbol)
            r  = self.extended.calculate_execution_cost(ob, order_size_usd, market=config.extended_symbol)
            if r:
                r['symbol'] = config.extended_display_symbol or config.extended_symbol
            return 'extended', r, None
        except _FETCH_ERRORS:
            log.exception("Extended fetch error")
            return 'extended', None, None

    def _fetch_edgex(self, config, order_size_usd: float, direction: str) -> Tuple[str, Optional[Dict], Any]:
        if not config.edgex_contract_id:
            return 'edgex', None, None
        try:
            sym = config.edgex_symbol or config.symbol_key
            r   = self.edgex.calculate_execution_cost(
                config.edgex_contract_id, order_size_usd, direction=direction, symbol=sym,
            )
            return 'edgex', r, None
        except _FETCH_ERRORS:
            log.exception("EdgeX fetch error")
            return 'edgex', None, None

    def _fetch_grvt(self, config, order_size_usd: float, direction: str) -> Tuple[str, Optional[Dict], Any]:
        if not config.grvt_instrument:
            return (
                'grvt',
                None,
                {'code': 'not_configured_for_asset', 'message': 'No grvt_instrument in asset registry.'},
            )
        try:
            r, grvt_dbg = self.grvt.calculate_execution_cost(
                config.grvt_instrument, order_size_usd, direction=direction, symbol=config.grvt_instrument,
            )
            return 'grvt', r, grvt_dbg
        except _FETCH_ERRORS:
            log.exception("GRVT fetch error")
            return 'grvt', None, None

    def _fetch_variational(self, asset_key: str, order_size_usd: float, is_long: bool) -> Tuple[str, Optional[Dict], Any]:
        try:
            r = self.variational.calculate_cost(asset_key, order_size_usd, is_long=is_long)
            return 'variational', r, None
        except _FETCH_ERRORS:
            log.exception("Variational fetch error")
            return 'variational', None, None

    def _fetch_ondo(self, config, order_size_usd: float) -> Tuple[str, Optional[Dict], Any]:
        if not config.ondo_symbol:
            return 'ondo', None, None
        try:
            ob = self.ondo.get_orderbook(config.ondo_symbol)
            r  = self.ondo.calculate_execution_cost(ob, order_size_usd, symbol=config.ondo_symbol)
            if r:
                r['symbol'] = config.ondo_symbol
            return 'ondo', r, None
        except _FETCH_ERRORS:
            log.exception("Ondo fetch error")
            return 'ondo', None, None

    def _fetch_pacifica(self, config, order_size_usd: float) -> Tuple[str, Optional[Dict], Any]:
        if not config.pacifica_symbol:
            return 'pacifica', None, None
        try:
            r = self.pacifica.get_optimal_execution(config.pacifica_symbol, order_size_usd)
            if r:
                r['symbol'] = config.pacifica_symbol
            return 'pacifica', r, None
        except _FETCH_ERRORS:
            log.exception("Pacifica fetch error")
            return 'pacifica', None, None

    def _apply_fetch_result(self, result: Dict, res: Tuple[str, Optional[Dict], Any]) -> None:
        name, payload, extra = res
        result[name] = payload
        if extra is None:
            return
        if name == 'grvt':
            result['grvt_debug'] = extra
            return
        result['symbols'][name] = extra

    @staticmethod
    def _zero_maker_slippage(result: Dict) -> None:
        for name in MAKER_SLIP_VENUES:
            ex_data = result.get(name)
            if not ex_data:
                continue
            ex_data['slippage_bps']      = 0.0
            ex_data['buy_slippage_bps']  = 0.0
            ex_data['sell_slippage_bps'] = 0.0
            if 'buy'  in ex_data:
                ex_data['buy']['slippage_bps']  = 0.0
            if 'sell' in ex_data:
                ex_data['sell']['slippage_bps'] = 0.0

    def compare_asset(
        self,
        asset_key: str,
        order_size_usd: float,
        order_type: str = 'taker',
        direction: str  = 'long',
    ) -> Optional[Dict]:
        """
        Compare execution cost across all exchanges for a given asset.

        Args:
            asset_key:      Asset symbol (e.g. 'XAU', 'NVDA')
            order_size_usd: Order size in USD
            order_type:     'taker' or 'maker'
            direction:      'long' or 'short'
        """
        if asset_key not in ASSETS:
            return None

        config  = ASSETS[asset_key]
        is_long = (direction.lower() == 'long')
        result  = self._blank_result(
            config, order_size_usd, order_type, direction,
            self.variational.VARIATIONAL_SYMBOLS.get(asset_key.upper()),
        )

        tasks = [
            lambda: self._fetch_hyperliquid(config, order_size_usd),
            lambda: self._fetch_lighter(config, order_size_usd),
            lambda: self._fetch_aster(config, order_size_usd),
            lambda: self._fetch_avantis(config, asset_key, order_size_usd, is_long),
            lambda: self._fetch_ostium(config, order_size_usd, is_long),
            lambda: self._fetch_extended(config, order_size_usd),
            lambda: self._fetch_edgex(config, order_size_usd, direction),
            lambda: self._fetch_grvt(config, order_size_usd, direction),
            lambda: self._fetch_variational(asset_key, order_size_usd, is_long),
            lambda: self._fetch_ondo(config, order_size_usd),
            lambda: self._fetch_pacifica(config, order_size_usd),
        ]

        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            for future in as_completed([executor.submit(fn) for fn in tasks]):
                try:
                    self._apply_fetch_result(result, future.result())
                except (TypeError, ValueError, KeyError, OSError):
                    log.exception("Exchange fetch error")

        if order_type == 'maker':
            self._zero_maker_slippage(result)

        return result

    @staticmethod
    def _fees_or_none(getter, key) -> Tuple[Optional[float], Optional[float]]:
        return getter(key) if key else (None, None)

    def _book_fee_structure(self, config, order_type: str) -> Dict:
        pairs = {
            'hyperliquid': self._fees_or_none(self.hyperliquid.get_fees, config.hyperliquid_symbol),
            'lighter':     self._fees_or_none(self.lighter.get_fees,     config.lighter_market_id),
            'aster':       self._fees_or_none(self.aster.get_fees,       config.aster_symbol),
            'extended':    self._fees_or_none(self.extended.get_fees,    config.extended_symbol),
            'edgex':       self._fees_or_none(self.edgex.get_fees,       config.edgex_contract_id),
            'grvt':        self._fees_or_none(self.grvt.get_fees,        config.grvt_instrument),
            'ondo':        self._fees_or_none(self.ondo.get_fees,        config.ondo_symbol),
            'pacifica':    self._fees_or_none(self.pacifica.get_fees,    config.pacifica_symbol),
        }
        use_maker = order_type == 'maker'
        structure = {}
        for name, (taker, maker) in pairs.items():
            fee = maker if use_maker else taker
            structure[name] = {'open': fee, 'close': fee}
        return structure

    @staticmethod
    def _overlay_oracle_fees(result: Dict, fee_structure: Dict) -> None:
        os_data = result.get('ostium')
        if os_data:
            fee_structure['ostium'] = {'open': os_data.get('fee_bps'), 'close': 0.0}
        av = result.get('avantis')
        if av:
            fee_structure['avantis'] = {
                'open': av.get('open_fee_bps', 0), 'close': av.get('close_fee_bps', 0),
            }
        var_data = result.get('variational')
        if var_data:
            fee_structure['variational'] = {
                'open':  var_data.get('open_fee_bps', 0.0),
                'close': var_data.get('close_fee_bps', 0.0),
            }

    @staticmethod
    def _apply_direction_slippages(result: Dict, is_long: bool) -> None:
        for name in DIRECTION_SLIP_VENUES:
            ex_data = result.get(name)
            if not ex_data:
                continue
            buy_slip  = round(_finite(ex_data.get('buy_slippage_bps')),  3)
            sell_slip = round(_finite(ex_data.get('sell_slippage_bps')), 3)
            ex_data['opening_slippage_bps'] = buy_slip  if is_long else sell_slip
            ex_data['closing_slippage_bps'] = sell_slip if is_long else buy_slip
            ex_data['slippage_type']         = 'opening_closing'

    @staticmethod
    def _score_exchange(name: str, ex_data: Dict, fee_structure: Dict, ranked: list) -> None:
        if ex_data.get('max_leverage') is not None:
            ex_data['max_leverage'] = floor_leverage(ex_data['max_leverage'])
        fees     = fee_structure.get(name, {'open': None, 'close': None})
        slippage = ex_data.get('slippage_bps', 0)
        if slippage is None:
            ex_data['total_cost_bps'] = None
            ex_data['exchange']       = name
            ex_data['filled']         = False
            return
        slippage         = _finite(slippage)
        effective_spread = slippage if name == 'avantis' else 2 * slippage
        f_open, f_close  = fees['open'], fees['close']
        if f_open is None or f_close is None:
            return
        total_cost = round(effective_spread + f_open + f_close, 3)
        ex_data['effective_spread_bps'] = round(effective_spread, 3)
        ex_data['open_fee_bps']         = round(f_open, 3)
        ex_data['close_fee_bps']        = round(f_close, 3)
        ex_data['total_cost_bps']       = total_cost
        ex_data['exchange']             = name
        if ex_data.get('executed') != 'PARTIAL':
            ranked.append({'name': name, 'total_cost': total_cost, 'filled': ex_data.get('filled', True)})

    def calculate_totals_and_winner(
        self, result: Dict, asset_key: str, order_type: str = 'taker', direction: str = 'long'
    ) -> Dict:
        """Calculate total costs and determine winner for a comparison result."""
        if not result or asset_key not in ASSETS:
            return result

        fee_structure = self._book_fee_structure(ASSETS[asset_key], order_type)
        self._overlay_oracle_fees(result, fee_structure)
        self._apply_direction_slippages(result, direction == 'long')

        ranked = []
        for name in ALL_VENUES:
            ex_data = result.get(name)
            if ex_data:
                self._score_exchange(name, ex_data, fee_structure, ranked)

        if ranked:
            winner = min(ranked, key=lambda x: x['total_cost'])
            result['winner']          = winner['name']
            result['winner_cost_bps'] = round(winner['total_cost'], 3)
        return result
