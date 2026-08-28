#!/usr/bin/env python3
"""
models.py - Shared data models, asset configuration, and execution calculator.

Contains:
  - AssetConfig / ASSETS  (symbol registry)
  - StandardizedOrderbook  (common orderbook format)
  - ExecutionCalculator    (shared slippage & cost logic)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


def floor_leverage(value) -> Optional[int]:
    """Floor max leverage to a whole number. None/invalid stays None."""
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(n) or math.isinf(n) or n <= 0:
        return None
    return int(math.floor(n))


# ---------------------------------------------------------------------------
# Hyperliquid protocol-level constants (not available via API)
# Source: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
# ---------------------------------------------------------------------------
HYPERLIQUID_GROWTH_MODE_SCALE = 0.1   # 90% fee reduction when growth mode enabled
HYPERLIQUID_NO_GROWTH_MODE_SCALE = 1.0  # No reduction when growth mode disabled


# ---------------------------------------------------------------------------
# Asset registry  –  MAG7 + COIN + Commodities + Forex
# ---------------------------------------------------------------------------
@dataclass
class AssetConfig:
    name: str
    symbol_key: str
    asset_class: str          # 'commodity', 'forex', 'index', 'stock'
    hyperliquid_symbol: Optional[str]
    lighter_market_id: Optional[int]
    aster_symbol: Optional[str]
    ostium_symbol: Optional[str]
    extended_symbol: Optional[str] = None   # Extended Exchange (Starknet)
    edgex_contract_id: Optional[int] = None # EdgeX contract ID
    edgex_symbol: Optional[str] = None      # EdgeX display symbol override (if different from symbol_key)
    grvt_instrument: Optional[str] = None   # GRVT instrument name (e.g. XAU_USDT_Perp)
    extended_display_symbol: Optional[str] = None  # Extended display symbol override (if different from extended_symbol)
    ondo_symbol: Optional[str] = None       # Ondo Perps market (e.g. NVDA-USD.P)
    pacifica_symbol: Optional[str] = None   # Pacifica API symbol (e.g. XAU; UI GOLD is an alias)


# Every entry below is available on Avantis (see AvantisAPI.PAIRS). Cross-venue
# symbols are only populated where the venue actually lists the ticker with a
# live book — a `None` means "this venue doesn't offer it", not "unknown".
#
# Extended's stock markets migrated to the `<TICKER>_24_5-USD` naming (24/5 US
# equity trading) — the legacy `<TICKER>-USD` symbols still resolve but return
# empty books, so we point at the live venue slug directly.
ASSETS: Dict[str, AssetConfig] = {
    # Commodities                                                                                     
    'XAU':    AssetConfig('XAU/USD',  'XAU',    'commodity', 'GOLD',   92,  'XAUUSDT',  'XAUUSD',  'XAU-USD',    30000005, None,         'XAU_USDT_Perp', ondo_symbol='XAU-USD.P', pacifica_symbol='XAU'),
    'XAG':    AssetConfig('XAG/USD',  'XAG',    'commodity', 'SILVER', 93,  'XAGUSDT',  'XAGUSD',  'XAG-USD',    30000006, None,         'XAG_USDT_Perp', ondo_symbol='XAG-USD.P', pacifica_symbol='XAG'),
    'WTI':    AssetConfig('WTI/USD',  'WTI',    'commodity', 'CL',           145, 'CLUSDT', 'CLUSD',     'WTI-USD', 30000008, None, ondo_symbol='WTI-USD.P', pacifica_symbol='CL'),
    'BRENT':  AssetConfig('BRENT/USD','BRENT',  'commodity', 'BRENTOIL',     159, None,     'BRENTUSD',  'XBR-USD', 30000017, ondo_symbol='BRENT-USD.P'),

    # Forex
    'EURUSD': AssetConfig('EUR/USD',  'EURUSD', 'forex',     'EUR',    96,  None, 'EURUSD',  'EUR-USD',    None, None, None, pacifica_symbol='EURUSD'),
    'GBPUSD': AssetConfig('GBP/USD',  'GBPUSD', 'forex',     'GBP',    97,  None, 'GBPUSD',  None,         None, None, None),
    'USDJPY': AssetConfig('USD/JPY',  'USDJPY', 'forex',     'JPY',    98,  None, 'USDJPY',  'USDJPY-USD', None, None, None, pacifica_symbol='USDJPY'),
    'USDCAD': AssetConfig('USD/CAD',  'USDCAD', 'forex',     None,     100, None, 'USDCAD',  None,         None, None, None),
    'USDCHF': AssetConfig('USD/CHF',  'USDCHF', 'forex',     None,     99,  None, 'USDCHF',  None,         None, None, None),
    'USDSEK': AssetConfig('USD/SEK',  'USDSEK', 'forex',     None,     None,None, None,      None,         None, None, None),
    'AUDUSD': AssetConfig('AUD/USD',  'AUDUSD', 'forex',     None,     106, None, 'AUDUSD',  None,         None, None, None),
    'NZDUSD': AssetConfig('NZD/USD',  'NZDUSD', 'forex',     None,     107, None, 'NZDUSD',  None,         None, None, None),
    'USDSGD': AssetConfig('USD/SGD',  'USDSGD', 'forex',     None,     None,None, None,      None,         None, None, None),
    'USDTRY': AssetConfig('USD/TRY',  'USDTRY', 'forex',     None,     None,None, None,      None,         None, None, None),
    'USDCNH': AssetConfig('USD/CNH',  'USDCNH', 'forex',     None,     None,None, None,      None,         None, None, None),
    'USDINR': AssetConfig('USD/INR',  'USDINR', 'forex',     None,     None,None, None,      None,         None, None, None),
    'USDKRW': AssetConfig('USD/KRW',  'USDKRW', 'forex',     'KRW',    105, None, 'USDKRW',  None,         None, None, None),
    'USDMXN': AssetConfig('USD/MXN',  'USDMXN', 'forex',     None,     None,None, 'USDMXN',  None,         None, None, None),
    'USDZAR': AssetConfig('USD/ZAR',  'USDZAR', 'forex',     None,     None,None, None,      None,         None, None, None),
    'USDBRL': AssetConfig('USD/BRL',  'USDBRL', 'forex',     None,     None,None, None,      None,         None, None, None),
    'USDIDR': AssetConfig('USD/IDR',  'USDIDR', 'forex',     None,     None,None, None,      None,         None, None, None),
    'USDTWD': AssetConfig('USD/TWD',  'USDTWD', 'forex',     None,     None,None, None,      None,         None, None, None),

    # MAG7 Stocks (Extended symbols use the _24_5 suffix now)
    'AAPL':   AssetConfig('AAPL/USD', 'AAPL',   'stock',     'AAPL',  113, 'AAPLUSDT', 'AAPLUSD', 'AAPL_24_5-USD', 30000012, ondo_symbol='AAPL-USD.P'),
    'MSFT':   AssetConfig('MSFT/USD', 'MSFT',   'stock',     'MSFT',  115, 'MSFTUSDT', 'MSFTUSD', 'MSFT_24_5-USD', 30000029, ondo_symbol='MSFT-USD.P'),
    'GOOG':   AssetConfig('GOOG/USD', 'GOOG',   'stock',     'GOOGL', 116, 'GOOGUSDT', 'GOOGUSD', 'GOOG_24_5-USD', 30000022, ondo_symbol='GOOGL-USD.P', pacifica_symbol='GOOGL'),
    'AMZN':   AssetConfig('AMZN/USD', 'AMZN',   'stock',     'AMZN',  114, 'AMZNUSDT', 'AMZNUSD', 'AMZN_24_5-USD', 30000013, None, 'AMZN_USDT_Perp', ondo_symbol='AMZN-USD.P'),
    'META':   AssetConfig('META/USD', 'META',   'stock',     'META',  117, 'METAUSDT', 'METAUSD', 'META_24_5-USD', 30000028, ondo_symbol='META-USD.P'),
    'NVDA':   AssetConfig('NVDA/USD', 'NVDA',   'stock',     'NVDA',  110, 'NVDAUSDT', 'NVDAUSD', 'NVDA_24_5-USD', 30000020, ondo_symbol='NVDA-USD.P', pacifica_symbol='NVDA'),
    'TSLA':   AssetConfig('TSLA/USD', 'TSLA',   'stock',     'TSLA',  112, 'TSLAUSDT', 'TSLAUSD', 'TSLA_24_5-USD', 30000023, None, 'TSLA_USDT_Perp', ondo_symbol='TSLA-USD.P', pacifica_symbol='TSLA'),

    # Indices (Avantis tickers: US500 / US100). Lighter lists both the index
    # (US500=180 / US100=181) and the ETF wrappers (SPY=128 / QQQ=129) — we
    # point at the index. HL SP500/XYZ100, Ostium SPX/NDX; Aster/Ondo/EdgeX
    # still only list the SPY/QQQ ETFs.
    'US500':  AssetConfig('US500/USD', 'US500', 'index',     'SP500',  180, None,       'SPXUSD',  'SPX500m-USD',    30000010, extended_display_symbol='SPX/USD', ondo_symbol='SPY-USD.P', pacifica_symbol='SP500'),
    'US100':  AssetConfig('US100/USD', 'US100', 'index',     'XYZ100', 181, 'QQQUSDT',  'NDXUSD',  'TECH100m-USD',   30000011, extended_display_symbol='NDX/USD', ondo_symbol='QQQ-USD.P'),

    # Additional equities available on Avantis (added 2026-08)
    'COIN':   AssetConfig('COIN/USD', 'COIN',   'stock', 'COIN', 109,  None,       'COINUSD', 'COIN_24_5-USD', 30000025, None, 'COIN_USDT_Perp', ondo_symbol='COIN-USD.P'),
    'HOOD':   AssetConfig('HOOD/USD', 'HOOD',   'stock', 'HOOD', 108,  'HOODUSDT', 'HOODUSD', 'HOOD_24_5-USD', 30000027, None, 'HOOD_USDT_Perp', ondo_symbol='HOOD-USD.P', pacifica_symbol='HOOD'),
    'SPCX':   AssetConfig('SPCX/USD', 'SPCX',   'stock', 'SPCX', 194,  'SPCXUSDT', None,      'SPCX-USD',      30000076, None, 'SPCX_USDT_Perp', extended_display_symbol='SPCX/USD', ondo_symbol='SPCX-USD.P', pacifica_symbol='SPCX'),
    'AMD':    AssetConfig('AMD/USD',  'AMD',    'stock', 'AMD',  138,  'AMDUSDT',  'AMDUSD',  'AMD_24_5-USD',  30000021, None, 'AMD_USDT_Perp',  ondo_symbol='AMD-USD.P'),
    'INTC':   AssetConfig('INTC/USD', 'INTC',   'stock', 'INTC', 137,  'INTCUSDT', 'INTCUSD', 'INTC_24_5-USD', 30000032, None, 'INTC_USDT_Perp', ondo_symbol='INTC-USD.P'),
    'MU':     AssetConfig('MU/USD',   'MU',     'stock', 'MU',   164,  'MUUSDT',   'MUUSD',   'MU_24_5-USD',   30000035, None, 'MU_USDT_Perp',   ondo_symbol='MU-USD.P', pacifica_symbol='MU'),
    'SNDK':   AssetConfig('SNDK/USD', 'SNDK',   'stock', 'SNDK', 139,  'SNDKUSDT', 'SNDKUSD', 'SNDK_24_5-USD', 30000034, None, 'SNDK_USDT_Perp', ondo_symbol='SNDK-USD.P', pacifica_symbol='SNDK'),
    'CBRS':   AssetConfig('CBRS/USD', 'CBRS',   'stock', 'CBRS', 175,  'CBRSUSDT', None,      'CBRS_24_5-USD', 30000078, None, 'CBRS_USDT_Perp', ondo_symbol='CBRS-USD.P'),
    'EWY':    AssetConfig('EWY/USD',  'EWY',    'stock', 'EWY',  166,  'EWYUSDT',  None,      'EWY_24_5-USD',  30000018, None, 'EWY_USDT_Perp',  ondo_symbol='EWY-USD.P'),
    'BABA':   AssetConfig('BABA/USD', 'BABA',   'stock', 'BABA', 177,  'BABAUSDT', None,      'BABA_24_5-USD', None,     None, 'BABA_USDT_Perp', ondo_symbol='BABA-USD.P'),
    'CRCL':   AssetConfig('CRCL/USD', 'CRCL',   'stock', 'CRCL', 121,  'CRCLUSDT', 'CRCLUSD', 'CRCL_24_5-USD', 30000026, None, 'CRCL_USDT_Perp', ondo_symbol='CRCL-USD.P', pacifica_symbol='CRCL'),
    'MRVL':   AssetConfig('MRVL/USD', 'MRVL',   'stock', 'MRVL', 174,  'MRVLUSDT', None,      'MRVL-USD',      30000077, None, 'MRVL_USDT_Perp', ondo_symbol='MRVL-USD.P'),
    'MSTR':   AssetConfig('MSTR/USD', 'MSTR',   'stock', 'MSTR', 122,  'MSTRUSDT', 'MSTRUSD', 'MSTR_24_5-USD', 30000014, None, 'MSTR_USDT_Perp', ondo_symbol='MSTR-USD.P', pacifica_symbol='MSTR'),
    'BB':     AssetConfig('BB/USD',   'BB',     'stock', 'BB',   211,  'BBUSDT',   'BBUSD',   'BB-USD',        None,     None, None,             ondo_symbol='BB-USD.P'),
    'PLTR':   AssetConfig('PLTR/USD', 'PLTR',   'stock', 'PLTR', 111,  'PLTRUSDT', 'PLTRUSD', 'PLTR_24_5-USD', 30000030, None, 'PLTR_USDT_Perp', ondo_symbol='PLTR-USD.P', pacifica_symbol='PLTR'),
    'AVGO':   AssetConfig('AVGO/USD', 'AVGO',   'stock', 'AVGO', 210,  'AVGOUSDT', 'AVGOUSD', 'AVGO-USD',      30000024, None, 'AVGO_USDT_Perp', ondo_symbol='AVGO-USD.P'),
    'NFLX':   AssetConfig('NFLX/USD', 'NFLX',   'stock', 'NFLX', None, 'NFLXUSDT', 'NFLXUSD', None,            None,     None, 'NFLX_USDT_Perp', ondo_symbol='NFLX-USD.P'),
    'CRWV':   AssetConfig('CRWV/USD', 'CRWV',   'stock', 'CRWV', 167,  'CRWVUSDT', None,      'CRWV-USD',      30000031, None, 'CRWV_USDT_Perp', ondo_symbol='CRWV-USD.P'),
    'SKHY':   AssetConfig('SKHY/USD', 'SKHY',   'stock', 'SKHY', 216,  None,       None,      'SKHYNIX-USD',   30000039, None, 'SKHY_USDT_Perp', ondo_symbol='SKHY-USD.P', pacifica_symbol='SKHYNIX'),
}


# ---------------------------------------------------------------------------
# StandardizedOrderbook
# ---------------------------------------------------------------------------
@dataclass
class StandardizedOrderbook:
    """
    Common orderbook format used by all exchanges.

    All exchange APIs normalize their raw data to this format before passing
    it to ExecutionCalculator.
    """
    bids: List[Dict[str, float]]   # [{'price': float, 'qty': float}, ...] best→worst
    asks: List[Dict[str, float]]   # [{'price': float, 'qty': float}, ...] best→worst (lowest first)
    best_bid: float
    best_ask: float
    mid_price: float
    timestamp: float = 0.0
    max_leverage: Optional[float] = None


# ---------------------------------------------------------------------------
# ExecutionCalculator  –  shared slippage & cost logic
# ---------------------------------------------------------------------------
class ExecutionCalculator:
    """
    Shared calculation logic for all orderbook-based exchanges.

    Formulas:
        mid_price            = (best_bid + best_ask) / 2
        avg_execution_price  = total_cost / total_qty   (walk the book)
        slippage_bps         = abs((avg_execution_price - mid_price) / mid_price) * 10000
        total_cost_bps       = (2 * slippage_bps) + open_fee_bps + close_fee_bps
    """

    @staticmethod
    def calculate_execution_cost(
        orderbook: 'StandardizedOrderbook',
        order_size_usd: float,
        open_fee_bps: float = 0.0,
        close_fee_bps: float = 0.0,
    ) -> Optional[Dict]:
        """
        Calculate execution cost from a standardized orderbook.

        Args:
            orderbook:       Standardized orderbook with bids/asks
            order_size_usd:  Order size in USD
            open_fee_bps:    Opening fee in basis points
            close_fee_bps:   Closing fee in basis points

        Returns:
            Standardized result dict with slippage, fees, and execution details.
        """
        if not orderbook or not orderbook.bids or not orderbook.asks:
            return None

        mid_price = orderbook.mid_price

        buy_result  = ExecutionCalculator._walk_book(orderbook.asks, order_size_usd, mid_price, side='buy')
        sell_result = ExecutionCalculator._walk_book(orderbook.bids, order_size_usd, mid_price, side='sell')

        if not buy_result or not sell_result:
            return None

        buy_slippage_bps  = buy_result['slippage_bps']
        sell_slippage_bps = sell_result['slippage_bps']
        avg_slippage_bps  = (buy_slippage_bps + sell_slippage_bps) / 2
        filled = buy_result['filled'] and sell_result['filled']

        buy_unfilled  = buy_result['unfilled_usd']
        sell_unfilled = sell_result['unfilled_usd']
        buy_partial   = not buy_result['filled']
        sell_partial  = not sell_result['filled']

        if buy_partial and sell_partial:
            unfilled_side = 'both'
        elif buy_partial:
            unfilled_side = 'buy'
        elif sell_partial:
            unfilled_side = 'sell'
        else:
            unfilled_side = None

        total_cost_bps = avg_slippage_bps + open_fee_bps + close_fee_bps

        return {
            'executed':          True if filled else 'PARTIAL',
            'mid_price':         mid_price,
            'best_bid':          orderbook.best_bid,
            'best_ask':          orderbook.best_ask,
            'slippage_bps':      avg_slippage_bps,
            'buy_slippage_bps':  buy_slippage_bps,
            'sell_slippage_bps': sell_slippage_bps,
            'open_fee_bps':      open_fee_bps,
            'close_fee_bps':     close_fee_bps,
            'total_cost_bps':    total_cost_bps,
            'filled':            filled,
            'order_size_usd':    order_size_usd,
            'filled_usd':        min(buy_result['filled_usd'], sell_result['filled_usd']),
            'unfilled_usd':      max(buy_unfilled, sell_unfilled),
            'unfilled_side':     unfilled_side,
            'buy':               buy_result,
            'sell':              sell_result,
            'timestamp':         orderbook.timestamp,
        }

    @staticmethod
    def _walk_book(
        levels: List[Dict[str, float]],
        order_size_usd: float,
        mid_price: float,
        side: str = 'buy',
    ) -> Optional[Dict]:
        """
        Walk through orderbook levels to fill an order.

        Args:
            levels:          List of {'price': float, 'qty': float} dicts
            order_size_usd:  Order size in USD
            mid_price:       Mid price for slippage calculation
            side:            'buy' or 'sell'

        Returns:
            Execution result with avg_price, slippage_bps, filled status.
        """
        if not levels:
            return None

        sorted_levels = sorted(levels, key=lambda x: x['price'], reverse=(side == 'sell'))

        unfilled_order_amount_usd = order_size_usd
        total_qty   = 0.0
        total_cost  = 0.0
        levels_used = 0

        for level in sorted_levels:
            price = level['price']
            qty   = level['qty']

            if price <= 0 or qty <= 0:
                continue

            value_available = price * qty

            if unfilled_order_amount_usd <= value_available:
                qty_needed = unfilled_order_amount_usd / price
                total_qty  += qty_needed
                total_cost += unfilled_order_amount_usd
                unfilled_order_amount_usd = 0
                levels_used += 1
                break
            else:
                total_qty  += qty
                total_cost += value_available
                unfilled_order_amount_usd -= value_available
                levels_used += 1

        filled_usd = order_size_usd - unfilled_order_amount_usd
        avg_price  = total_cost / total_qty if total_qty > 0 else 0
        slippage_bps = abs((avg_price - mid_price) / mid_price) * 10000 if mid_price > 0 else 0

        return {
            'filled':       unfilled_order_amount_usd == 0,
            'filled_usd':   filled_usd,
            'unfilled_usd': unfilled_order_amount_usd,
            'levels_used':  levels_used,
            'avg_price':    avg_price,
            'slippage_bps': slippage_bps,
        }

    @staticmethod
    def calculate_hybrid_execution_cost(
        primary_book: 'StandardizedOrderbook',
        secondary_book: 'StandardizedOrderbook',
        order_size_usd: float,
        open_fee_bps: float = 0.0,
        close_fee_bps: float = 0.0,
    ) -> Optional[Dict]:
        """
        Fill from primary book first, then spill into secondary book.
        "Stitch" logic: fill what you can from Primary, remainder from Secondary.
        """
        if not primary_book and not secondary_book:
            return None
        if not primary_book:
            return ExecutionCalculator.calculate_execution_cost(secondary_book, order_size_usd, open_fee_bps, close_fee_bps)
        if not secondary_book:
            return ExecutionCalculator.calculate_execution_cost(primary_book, order_size_usd, open_fee_bps, close_fee_bps)

        mid_price = primary_book.mid_price

        def walk_hybrid(prim_levels, sec_levels, side):
            prim_res = ExecutionCalculator._walk_book(prim_levels, order_size_usd, mid_price, side)

            if prim_res['filled']:
                return prim_res

            unfilled     = prim_res['unfilled_usd']
            filled_amount = prim_res['filled_usd']
            avg_prim     = prim_res['avg_price'] if prim_res['avg_price'] else 0
            cost_prim    = filled_amount
            qty_prim     = filled_amount / avg_prim if avg_prim > 0 else 0

            sorted_prim  = sorted(prim_levels, key=lambda x: x['price'], reverse=(side == 'sell'))
            levels_used  = prim_res.get('levels_used', 0)

            last_prim_price = 0
            if levels_used > 0 and levels_used <= len(sorted_prim):
                last_prim_price = sorted_prim[levels_used - 1]['price']
            else:
                last_prim_price = sorted_prim[-1]['price'] if sorted_prim else 0

            qty_at_boundary = sum(l['qty'] for l in prim_levels if l['price'] == last_prim_price)

            filtered_sec = []
            for lvl in sec_levels:
                price        = lvl['price']
                qty          = lvl['qty']
                include      = False
                adjusted_qty = qty

                if side == 'buy':
                    if price > last_prim_price:
                        include = True
                    elif price == last_prim_price:
                        adjusted_qty = max(0, qty - qty_at_boundary)
                        if adjusted_qty > 0:
                            include = True
                elif side == 'sell':
                    if price < last_prim_price:
                        include = True
                    elif price == last_prim_price:
                        adjusted_qty = max(0, qty - qty_at_boundary)
                        if adjusted_qty > 0:
                            include = True

                if include:
                    new_lvl        = lvl.copy()
                    new_lvl['qty'] = adjusted_qty
                    filtered_sec.append(new_lvl)

            sec_res = ExecutionCalculator._walk_book(filtered_sec, unfilled, mid_price, side)

            if sec_res['filled']:
                cost_sec  = sec_res['filled_usd']
                qty_sec   = sec_res['filled_usd'] / sec_res['avg_price'] if sec_res['avg_price'] > 0 else 0
                total_qty = qty_prim + qty_sec
                total_cost = cost_prim + cost_sec
                final_avg  = total_cost / total_qty if total_qty > 0 else 0
                slip       = abs((final_avg - mid_price) / mid_price) * 10000

                return {
                    'filled':       True,
                    'filled_usd':   order_size_usd,
                    'avg_price':    final_avg,
                    'slippage_bps': slip,
                }
            else:
                return {'filled': False, 'slippage_bps': 0}

        buy_result  = walk_hybrid(primary_book.asks, secondary_book.asks, 'buy')
        sell_result = walk_hybrid(primary_book.bids, secondary_book.bids, 'sell')

        if not buy_result or not sell_result:
            return None

        buy_slippage_bps  = buy_result['slippage_bps']
        sell_slippage_bps = sell_result['slippage_bps']
        avg_slippage_bps  = (buy_slippage_bps + sell_slippage_bps) / 2
        filled            = buy_result['filled'] and sell_result['filled']
        total_cost_bps    = avg_slippage_bps + open_fee_bps + close_fee_bps

        return {
            'executed':          True if filled else 'PARTIAL',
            'mid_price':         mid_price,
            'slippage_bps':      avg_slippage_bps,
            'buy_slippage_bps':  buy_slippage_bps,
            'sell_slippage_bps': sell_slippage_bps,
            'open_fee_bps':      open_fee_bps,
            'close_fee_bps':     close_fee_bps,
            'total_cost_bps':    total_cost_bps,
            'filled':            filled,
            'buy':               buy_result,
            'sell':              sell_result,
            'timestamp':         primary_book.timestamp,
        }
