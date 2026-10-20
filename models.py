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


# Entries are quoted on Veranta when the key is in VerantaAPI.PAIRS; the
# /USD Korean equities below are the exception (Veranta only lists them in
# KRW, as separate *KRW assets). Cross-venue symbols are only populated where the venue actually lists the ticker with a
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

    # Indices (Veranta tickers: US500 / US100). Lighter lists both the index
    # (US500=180 / US100=181) and the ETF wrappers (SPY=128 / QQQ=129) — we
    # point at the index. HL SP500/XYZ100, Ostium SPX/NDX; Aster/Ondo/EdgeX
    # still only list the SPY/QQQ ETFs.
    'US500':  AssetConfig('US500/USD', 'US500', 'index',     'SP500',  180, None,       'SPXUSD',  'SPX500m-USD',    30000010, extended_display_symbol='SPX/USD', ondo_symbol='SPY-USD.P', pacifica_symbol='SP500'),
    'US100':  AssetConfig('US100/USD', 'US100', 'index',     'XYZ100', 181, 'QQQUSDT',  'NDXUSD',  'TECH100m-USD',   30000011, extended_display_symbol='NDX/USD', ondo_symbol='QQQ-USD.P'),

    # Additional equities available on Veranta (added 2026-08)
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
    # SKHY = US-listed SK Hynix (~$140). Venues also list the KRX share as
    # SKHYNIX (~$950, Pacifica / EdgeX 30000039 / Extended SKHYNIX-USD) —
    # a different instrument, so it must not be mapped here.
    'SKHY':   AssetConfig('SKHY/USD', 'SKHY',   'stock', 'SKHY', 216,  'SKHYUSDT', 'SKHYUSD', 'SKHY-USD',      30000087, None, 'SKHY_USDT_Perp', ondo_symbol='SKHY-USD.P'),

    # Equities listed on Veranta 2026-09/10 (pairs 120–184). Ticker collisions
    # with crypto are left unmapped: DASH (Dash) and STX (Stacks) on
    # Aster/Extended/Lighter are coins, not DoorDash / Seagate. Lighter ARM/BE/WDC
    # and Ondo BE/ADBE markets exist but have empty books.
    'LLY':    AssetConfig('LLY/USD',  'LLY',    'stock', 'LLY',  None, 'LLYUSDT',  'LLYUSD',  None,            None,     None, 'LLY_USDT_Perp'),
    'BE':     AssetConfig('BE/USD',   'BE',     'stock', 'BE',   None, 'BEUSDT',   None,      'BE-USD',        None,     None, 'BE_USDT_Perp'),
    'LITE':   AssetConfig('LITE/USD', 'LITE',   'stock', 'LITE', 178,  'LITEUSDT', None,      'LITE-USD',      30000033, None, 'LITE_USDT_Perp', ondo_symbol='LITE-USD.P'),
    'STRC':   AssetConfig('STRC/USD', 'STRC',   'stock', 'STRC', 156,  'STRCUSDT', None,      None,            None,     None, 'STRC_USDT_Perp'),
    'DELL':   AssetConfig('DELL/USD', 'DELL',   'stock', 'DELL', 187,  'DELLUSDT', 'DELLUSD', 'DELL-USD',      30000157, None, 'DELL_USDT_Perp'),
    'NBIS':   AssetConfig('NBIS/USD', 'NBIS',   'stock', 'NBIS', 189,  'NBISUSDT', 'NBISUSD', 'NBIS-USD',      30000142, None, 'NBIS_USDT_Perp', ondo_symbol='NBIS-USD.P'),
    'IREN':   AssetConfig('IREN/USD', 'IREN',   'stock', 'IREN', None, 'IRENUSDT', None,      'IREN_24_5-USD', None,     None, 'IREN_USDT_Perp'),
    'ARM':    AssetConfig('ARM/USD',  'ARM',    'stock', 'ARM',  None, 'ARMUSDT',  'ARMUSD',  'ARM-USD',       30000156, None, 'ARM_USDT_Perp',  ondo_symbol='ARM-USD.P'),
    'MRNA':   AssetConfig('MRNA/USD', 'MRNA',   'stock', 'MRNA', 228,  'MRNAUSDT', 'MRNAUSD', 'MRNA-USD',      30000159, None, 'MRNA_USDT_Perp', ondo_symbol='MRNA-USD.P'),
    'RKLB':   AssetConfig('RKLB/USD', 'RKLB',   'stock', 'RKLB', 186,  'RKLBUSDT', None,      'RKLB-USD',      30000036, None, 'RKLB_USDT_Perp'),
    'WDC':    AssetConfig('WDC/USD',  'WDC',    'stock', 'WDC',  None, 'WDCUSDT',  None,      'WDC-USD',       30000150, None, 'WDC_USDT_Perp'),
    'NOW':    AssetConfig('NOW/USD',  'NOW',    'stock', 'NOW',  191,  'NOWUSDT',  None,      None,            None,     None, 'NOW_USDT_Perp'),
    'AMGN':   AssetConfig('AMGN/USD', 'AMGN',   'stock', None,   None, None,       None,      'AMGN-USD',      None),
    'CSCO':   AssetConfig('CSCO/USD', 'CSCO',   'stock', None,   None, 'CSCOUSDT', None,      'CSCO_24_5-USD', None,     None, 'CSCO_USDT_Perp'),
    'KLAC':   AssetConfig('KLAC/USD', 'KLAC',   'stock', None,   None, None,       None,      'KLAC_24_5-USD', None),
    'LRCX':   AssetConfig('LRCX/USD', 'LRCX',   'stock', 'LRCX', None, None,       None,      'LRCX_24_5-USD', None),
    'TXN':    AssetConfig('TXN/USD',  'TXN',    'stock', None,   None, None,       None,      'TXN-USD',       None),
    'PANW':   AssetConfig('PANW/USD', 'PANW',   'stock', None,   None, None,       None,      None,            None),
    'APP':    AssetConfig('APP/USD',  'APP',    'stock', None,   None, None,       None,      'APP_24_5-USD',  None),
    'ALAB':   AssetConfig('ALAB/USD', 'ALAB',   'stock', None,   None, None,       None,      'ALAB_24_5-USD', None),
    'DDOG':   AssetConfig('DDOG/USD', 'DDOG',   'stock', None,   None, None,       None,      'DDOG-USD',      None),
    'CEG':    AssetConfig('CEG/USD',  'CEG',    'stock', None,   None, None,       None,      'CEG-USD',       None),
    'SNPS':   AssetConfig('SNPS/USD', 'SNPS',   'stock', None,   None, None,       None,      'SNPS-USD',      None),
    'CDNS':   AssetConfig('CDNS/USD', 'CDNS',   'stock', None,   None, None,       None,      'CDNS-USD',      None),
    'MPWR':   AssetConfig('MPWR/USD', 'MPWR',   'stock', None,   None, None,       None,      'MPWR-USD',      None),
    'FTNT':   AssetConfig('FTNT/USD', 'FTNT',   'stock', None,   None, None,       None,      'FTNT-USD',      None),
    'ADBE':   AssetConfig('ADBE/USD', 'ADBE',   'stock', None,   None, 'ADBEUSDT', None,      'ADBE_24_5-USD', None),
    'INTU':   AssetConfig('INTU/USD', 'INTU',   'stock', None,   None, None,       None,      'INTU-USD',      None),
    'ADI':    AssetConfig('ADI/USD',  'ADI',    'stock', None,   None, None,       None,      'ADI-USD',       None),
    'TER':    AssetConfig('TER/USD',  'TER',    'stock', None,   None, None,       None,      'TER_24_5-USD',  None),
    'NXPI':   AssetConfig('NXPI/USD', 'NXPI',   'stock', None,   None, None,       None,      'NXPI-USD',      None),
    'SHOP':   AssetConfig('SHOP/USD', 'SHOP',   'stock', None,   None, None,       None,      'SHOP-USD',      30000155),
    'WDAY':   AssetConfig('WDAY/USD', 'WDAY',   'stock', None,   None, None,       None,      None,            None),
    'ADSK':   AssetConfig('ADSK/USD', 'ADSK',   'stock', None,   None, None,       None,      'ADSK-USD',      None),
    'MCHP':   AssetConfig('MCHP/USD', 'MCHP',   'stock', None,   None, None,       None,      'MCHP-USD',      None),
    'BKNG':   AssetConfig('BKNG/USD', 'BKNG',   'stock', None,   None, None,       None,      'BKNG-USD',      None),
    'MELI':   AssetConfig('MELI/USD', 'MELI',   'stock', None,   None, None,       None,      'MELI-USD',      None),
    'ISRG':   AssetConfig('ISRG/USD', 'ISRG',   'stock', None,   None, None,       None,      'ISRG-USD',      None),
    'TMUS':   AssetConfig('TMUS/USD', 'TMUS',   'stock', None,   None, None,       None,      'TMUS-USD',      None),
    'ABNB':   AssetConfig('ABNB/USD', 'ABNB',   'stock', None,   None, None,       None,      'ABNB-USD',      None),
    'DASH':   AssetConfig('DASH/USD', 'DASH',   'stock', None,   None, None,       None,      None,            None),
    'PDD':    AssetConfig('PDD/USD',  'PDD',    'stock', None,   None, None,       None,      'PDD_24_5-USD',  30000163),
    'PYPL':   AssetConfig('PYPL/USD', 'PYPL',   'stock', None,   None, None,       None,      None,            30000122),
    'TTWO':   AssetConfig('TTWO/USD', 'TTWO',   'stock', None,   179,  None,       None,      'TTWO-USD',      None,     ondo_symbol='TTWO-USD.P'),
    'WMT':    AssetConfig('WMT/USD',  'WMT',    'stock', None,   None, 'WMTUSDT',  None,      'WMT-USD',       30000127, None, 'WMT_USDT_Perp'),
    'GILD':   AssetConfig('GILD/USD', 'GILD',   'stock', None,   None, None,       None,      None,            None),
    'VRTX':   AssetConfig('VRTX/USD', 'VRTX',   'stock', None,   None, None,       None,      'VRTX-USD',      None),
    'REGN':   AssetConfig('REGN/USD', 'REGN',   'stock', None,   None, None,       None,      'REGN-USD',      None),
    'LIN':    AssetConfig('LIN/USD',  'LIN',    'stock', None,   None, None,       None,      'LIN-USD',       None),
    'ADP':    AssetConfig('ADP/USD',  'ADP',    'stock', None,   None, None,       None,      None,            None),
    'HON':    AssetConfig('HON/USD',  'HON',    'stock', None,   None, None,       None,      'HON-USD',       None),
    'STX':    AssetConfig('STX/USD',  'STX',    'stock', None,   None, None,       None,      None,            None),
    'AXON':   AssetConfig('AXON/USD', 'AXON',   'stock', None,   None, None,       None,      'AXON-USD',      None),
    'VST':    AssetConfig('VST/USD',  'VST',    'stock', None,   None, None,       None,      None,            30000154),
    'ANET':   AssetConfig('ANET/USD', 'ANET',   'stock', None,   None, None,       None,      None,            None),
    'UBER':   AssetConfig('UBER/USD', 'UBER',   'stock', None,   None, 'UBERUSDT', None,      None,            None,     None, 'UBER_USDT_Perp'),
    'BA':     AssetConfig('BA/USD',   'BA',     'stock', None,   None, None,       None,      None,            None),
    'SOFI':   AssetConfig('SOFI/USD', 'SOFI',   'stock', None,   None, None,       None,      'SOFI-USD',      30000106),
    'GRAB':   AssetConfig('GRAB/USD', 'GRAB',   'stock', None,   None, None,       None,      None,            None),
    'ASTS':   AssetConfig('ASTS/USD', 'ASTS',   'stock', None,   None, 'ASTSUSDT', None,      'ASTS-USD',      None,     None, 'ASTS_USDT_Perp'),
    'BOT':    AssetConfig('BOT/USD',  'BOT',    'stock', 'BOT',  185,  None,       None,      None,            None),

    # Korean equities — /KRW pairs (Veranta 185–201, KRX feed). No other venue
    # lists these in KRW, so they are Veranta-only and must not be compared
    # against the /USD perps below.
    'SAMSUNGKRW':      AssetConfig('005930/KRW Samsung Electronics',       'SAMSUNGKRW',      'stock', None, None, None, None, None, None),
    'HYUNDAIKRW':      AssetConfig('005380/KRW Hyundai Motor',             'HYUNDAIKRW',      'stock', None, None, None, None, None, None),
    'NAVERKRW':        AssetConfig('035420/KRW NAVER',                     'NAVERKRW',        'stock', None, None, None, None, None, None),
    'LGELECKRW':       AssetConfig('066570/KRW LG Electronics',            'LGELECKRW',       'stock', None, None, None, None, None, None),
    'HANMIKRW':        AssetConfig('042700/KRW Hanmi Semiconductor',       'HANMIKRW',        'stock', None, None, None, None, None, None),
    'SAMSUNGEMKRW':    AssetConfig('009150/KRW Samsung Electro-Mechanics', 'SAMSUNGEMKRW',    'stock', None, None, None, None, None, None),
    'KODEX200KRW':     AssetConfig('069500/KRW KODEX 200',                 'KODEX200KRW',     'stock', None, None, None, None, None, None),
    'HYUNDAISTEELKRW': AssetConfig('004020/KRW Hyundai Steel',             'HYUNDAISTEELKRW', 'stock', None, None, None, None, None, None),
    'NHISKRW':         AssetConfig('005940/KRW NH Investment & Securities', 'NHISKRW',        'stock', None, None, None, None, None, None),
    'HDKSOEKRW':       AssetConfig('009540/KRW HD Korea Shipbuilding',     'HDKSOEKRW',       'stock', None, None, None, None, None, None),
    'HMMKRW':          AssetConfig('011200/KRW HMM',                       'HMMKRW',          'stock', None, None, None, None, None, None),
    'SKINCKRW':        AssetConfig('034730/KRW SK Inc',                    'SKINCKRW',        'stock', None, None, None, None, None, None),
    'KAKAOKRW':        AssetConfig('035720/KRW Kakao',                     'KAKAOKRW',        'stock', None, None, None, None, None, None),
    'LGCHEMKRW':       AssetConfig('051910/KRW LG Chem',                   'LGCHEMKRW',       'stock', None, None, None, None, None, None),
    'KRAFTONKRW':      AssetConfig('259960/KRW Krafton',                   'KRAFTONKRW',      'stock', None, None, None, None, None, None),
    'HANWHASYSKRW':    AssetConfig('272210/KRW Hanwha Systems',            'HANWHASYSKRW',    'stock', None, None, None, None, None, None),
    'LGESKRW':         AssetConfig('373220/KRW LG Energy Solution',        'LGESKRW',         'stock', None, None, None, None, None, None),

    # Korean equities — /USD perps on other venues (not listed on Veranta).
    # KODEX200 is the KOSPI 200 ETF; HL / Extended only list the KR200 index
    # itself, used as the closest proxy (same as US500 ↔ SPY on Ondo).
    'SAMSUNG':   AssetConfig('SAMSUNG/USD Samsung Electronics',       'SAMSUNG',   'stock', 'SMSN',    162,  'SAMSUNGUSDT', None, 'SAMSUNG-USD', 30000079, None, 'SAMSUNG_USDT_Perp', ondo_symbol='SMSN-USD.P', pacifica_symbol='SAMSUNG'),
    'HYUNDAI':   AssetConfig('HYUNDAI/USD Hyundai Motor',             'HYUNDAI',   'stock', 'HYUNDAI', 160,  'HYUNDAIUSDT', None, None,          None,     None, 'HYUNDAI_USDT_Perp'),
    'NAVER':     AssetConfig('NAVER/USD NAVER',                       'NAVER',     'stock', None,      None, None,          None, None,          30000145, None, 'NAVER_USDT_Perp'),
    'LGELEC':    AssetConfig('LGELEC/USD LG Electronics',             'LGELEC',    'stock', None,      None, None,          None, None,          30000146, None, 'LGELECTRONICS_USDT_Perp'),
    'HANMI':     AssetConfig('HANMI/USD Hanmi Semiconductor',         'HANMI',     'stock', None,      None, None,          None, None,          30000147, None, 'HANMI_USDT_Perp'),
    'SAMSUNGEM': AssetConfig('SAMSUNGEM/USD Samsung Electro-Mechanics', 'SAMSUNGEM', 'stock', None,    None, None,          None, None,          30000148, None, 'SAMSUNGEM_USDT_Perp'),
    'KODEX200':  AssetConfig('KODEX200/USD KODEX 200',                'KODEX200',  'stock', 'KR200',   None, None,          None, 'KR200-USD',   30000149, None, 'KODEX200_USDT_Perp', extended_display_symbol='KR200/USD'),
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
    def _boundary_price(prim_levels, prim_res, side: str) -> float:
        sorted_prim = sorted(prim_levels, key=lambda x: x['price'], reverse=(side == 'sell'))
        levels_used = prim_res.get('levels_used', 0)
        if levels_used > 0 and levels_used <= len(sorted_prim):
            return sorted_prim[levels_used - 1]['price']
        if sorted_prim:
            return sorted_prim[-1]['price']
        return 0

    @staticmethod
    def _adjust_secondary_level(price, qty, last_prim_price, qty_at_boundary, side):
        beyond = price > last_prim_price if side == 'buy' else price < last_prim_price
        if beyond:
            return qty, True
        if price != last_prim_price:
            return qty, False
        adjusted = max(0, qty - qty_at_boundary)
        return adjusted, adjusted > 0

    @staticmethod
    def _filter_secondary(sec_levels, last_prim_price, qty_at_boundary, side):
        filtered = []
        for lvl in sec_levels:
            adjusted_qty, include = ExecutionCalculator._adjust_secondary_level(
                lvl['price'], lvl['qty'], last_prim_price, qty_at_boundary, side,
            )
            if not include:
                continue
            new_lvl = lvl.copy()
            new_lvl['qty'] = adjusted_qty
            filtered.append(new_lvl)
        return filtered

    @staticmethod
    def _merge_hybrid_fills(prim_res, sec_res, order_size_usd, mid_price):
        if not sec_res['filled']:
            return {'filled': False, 'slippage_bps': 0}
        filled_amount = prim_res['filled_usd']
        avg_prim = prim_res['avg_price'] if prim_res['avg_price'] else 0
        qty_prim = filled_amount / avg_prim if avg_prim > 0 else 0
        qty_sec = sec_res['filled_usd'] / sec_res['avg_price'] if sec_res['avg_price'] > 0 else 0
        total_qty = qty_prim + qty_sec
        total_cost = filled_amount + sec_res['filled_usd']
        final_avg = total_cost / total_qty if total_qty > 0 else 0
        slip = abs((final_avg - mid_price) / mid_price) * 10000
        return {
            'filled': True,
            'filled_usd': order_size_usd,
            'avg_price': final_avg,
            'slippage_bps': slip,
        }

    @staticmethod
    def _walk_hybrid(prim_levels, sec_levels, order_size_usd, mid_price, side):
        prim_res = ExecutionCalculator._walk_book(prim_levels, order_size_usd, mid_price, side)
        if prim_res['filled']:
            return prim_res
        last_prim_price = ExecutionCalculator._boundary_price(prim_levels, prim_res, side)
        qty_at_boundary = sum(l['qty'] for l in prim_levels if l['price'] == last_prim_price)
        filtered_sec = ExecutionCalculator._filter_secondary(
            sec_levels, last_prim_price, qty_at_boundary, side,
        )
        sec_res = ExecutionCalculator._walk_book(filtered_sec, prim_res['unfilled_usd'], mid_price, side)
        return ExecutionCalculator._merge_hybrid_fills(prim_res, sec_res, order_size_usd, mid_price)

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
        buy_result  = ExecutionCalculator._walk_hybrid(
            primary_book.asks, secondary_book.asks, order_size_usd, mid_price, 'buy'
        )
        sell_result = ExecutionCalculator._walk_hybrid(
            primary_book.bids, secondary_book.bids, order_size_usd, mid_price, 'sell'
        )

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
