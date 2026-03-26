#!/usr/bin/env python3
"""
models.py - Shared data models, asset configuration, and execution calculator.

Contains:
  - AssetConfig / ASSETS  (symbol registry)
  - StandardizedOrderbook  (common orderbook format)
  - ExecutionCalculator    (shared slippage & cost logic)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


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
    avantis_x_instrument: Optional[str] = None  # LMAX instrument name for AvantisX (e.g. Gold (Spot))


ASSETS: Dict[str, AssetConfig] = {
    # Commodities                                                                                     
    'XAU':    AssetConfig('XAU/USD',  'XAU',    'commodity', 'GOLD',   92,  'XAUUSDT',  'XAUUSD',  'XAU-USD',    10000234, 'XAUT',       'XAU_USDT_Perp',  avantis_x_instrument='Gold (Spot)'),
    'XAG':    AssetConfig('XAG/USD',  'XAG',    'commodity', 'SILVER', 93,  'XAGUSDT',  'XAGUSD',  'XAG-USD',    10000278, 'SILVERUSDT', 'XAG_USDT_Perp',  avantis_x_instrument='Silver (Spot)'),
    'WTI':    AssetConfig('WTI/USD',  'WTI',    'commodity', 'CL',           145, 'CLUSDT', 'CLUSD',     'WTI-USD', 10000283, 'CLUSD'),
    'BRENT':  AssetConfig('BRENT/USD','BRENT',  'commodity', 'BRENTOIL',     146, None,     'BRENTUSD',  'XBR-USD'),

    # Forex
    'EURUSD': AssetConfig('EUR/USD',  'EURUSD', 'forex',     'EUR',   96,  None,       'EURUSD',  'EUR-USD',    None,     None,         None,             avantis_x_instrument='EUR/USD'),
    'GBPUSD': AssetConfig('GBP/USD',  'GBPUSD', 'forex',     'GBP',   97,  None,       'GBPUSD',  None,         None,     None,         None,             avantis_x_instrument='GBP/USD'),
    'USDJPY': AssetConfig('USD/JPY',  'USDJPY', 'forex',     'JPY',   98,  None,       'USDJPY',  'USDJPY-USD', None,     None,         None,             avantis_x_instrument='USD/JPY'),

    # MAG7 Stocks                                                                                     
    'AAPL':   AssetConfig('AAPL/USD', 'AAPL',   'stock',     'AAPL',  113, 'AAPLUSDT', 'AAPLUSD', None,         10000275),
    'MSFT':   AssetConfig('MSFT/USD', 'MSFT',   'stock',     'MSFT',  115, 'MSFTUSDT', 'MSFTUSD', None,         None),
    'GOOG':   AssetConfig('GOOG/USD', 'GOOG',   'stock',     'GOOGL', 116, 'GOOGUSDT', 'GOOGUSD', None,         10000280),
    'AMZN':   AssetConfig('AMZN/USD', 'AMZN',   'stock',     'AMZN',  114, 'AMZNUSDT', 'AMZNUSD', None,         10000281, None, 'AMZN_USDT_Perp'),
    'META':   AssetConfig('META/USD', 'META',   'stock',     'META',  117, 'METAUSDT', 'METAUSD', None,         10000290),
    'NVDA':   AssetConfig('NVDA/USD', 'NVDA',   'stock',     'NVDA',  110, 'NVDAUSDT', 'NVDAUSD', None,         10000272),
    'TSLA':   AssetConfig('TSLA/USD', 'TSLA',   'stock',     'TSLA',  112, 'TSLAUSDT', 'TSLAUSD', None,         10000273, None, 'TSLA_USDT_Perp'),

    # Indices
    'SPY':    AssetConfig('SPY/USD',  'SPY',    'index',     None,    128, None,       'SPYUSD',  None,         10000248),
    'QQQ':    AssetConfig('QQQ/USD',  'QQQ',    'index',     None,    129, 'QQQUSDT',  'QQQUSD',  None,         10000249),

    # Other
    'COIN':   AssetConfig('COIN/USD', 'COIN',   'stock',     'COIN',  109, None,       'COINUSD', None,         10000274, None, 'COIN_USDT_Perp'),
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
