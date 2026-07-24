# RWA Fee Comparison — Backend

Multi-exchange slippage & fee comparison API for Real-World Asset (RWA) perp DEXs.

---

## Supported Assets

| Key | Pair | Class |
|---|---|---|
| `XAU` / `XAG` | XAU/USD · XAG/USD | Commodity |
| `EURUSD` / `GBPUSD` / `USDJPY` | EUR · GBP · JPY | Forex |
| `AAPL` `MSFT` `GOOG` `AMZN` `META` `NVDA` `TSLA` `COIN` | MAG7 + COIN | Stock |
| `SPY` / `QQQ` | — | Index |

---

## Exchange Overview

| Exchange | Chain | Book Type |
|---|---|---|
| **Hyperliquid** | Arbitrum L1 (`xyz` dex) | Real L2 orderbook |
| **Lighter** | zkSync | Real L2 orderbook |
| **Aster** | BSC/CEX-style | Real L2 orderbook |
| **Extended** | Starknet | Real L2 orderbook |
| **Avantis** | Base | Oracle + dynamic spread |
| **Ostium** | Arbitrum | Oracle + dynamic spread |

---

## Calculation Processes

All costs in **basis points (bps)** — `1 bps = 0.01%`.

---

### 1. Spread / Slippage

#### Orderbook Exchanges (Hyperliquid, Lighter, Aster, Extended)

All four use the same shared logic — walk the book to fill the order:

```
mid_price           = (best_bid + best_ask) / 2
avg_fill_price      = total_cost_filled / total_qty_filled
slippage_bps        = abs((avg_fill_price - mid_price) / mid_price) × 10,000
```

Buy side walks asks (lowest first); sell side walks bids (highest first). Average of both = `slippage_bps`.

---

#### Lighter — Orderbook Details

| | |
|---|---|
| Endpoint | `GET /orderBookOrders?market_id=<id>&limit=250` |
| Qty field | `remaining_base_amount` |
| Fees | `GET /orderBookDetails` → `taker_fee × 100` → bps |
| Max leverage | `10000 / min_initial_margin_fraction` |
| Funding | 8H rate `/funding-rates` → ÷8 for 1H, ×3 for 24H |

#### Aster — Orderbook Details

| | |
|---|---|
| Endpoint | `GET /fapi/v1/depth?symbol=<sym>&limit=1000` |
| Format | `bids[][0]` = price, `bids[][1]` = qty |
| Fees | Authenticated `/commissionRate` (`ASTER_API_KEY` + `ASTER_SECRET_KEY`) |
| Fee convention | `takerCommissionRate × 10000` bps (per-leg; charged once on open, once on close — no extra doubling) |
| Max leverage | Max key in `leverageOiRemainingMap` |
| Funding | 4H rate `/real-time-funding-rate` → ÷4 for 1H, ×6 for 24H |

#### Extended (Starknet) — Orderbook Details

| | |
|---|---|
| Endpoint | `GET /api/v1/info/markets/<market>/orderbook` |
| Fields | `bid[].price` + `bid[].qty` / `ask[].price` + `ask[].qty` |
| Fees | `GET /api/v1/user/fees?market=<m>` → `takerFeeRate × 10000` bps (`EXTENDED_API_KEY`) |
| Max leverage | `tradingConfig.maxLeverage` from `/info/markets` |
| Funding | 1H rate from `/info/markets/<m>/stats` → `fundingRate × 100` |

#### Hyperliquid — Precision Cascade

Hyperliquid `xyz` has thin RWA orderbooks; uses a 3-step cascade:

```
Step 1  Fetch default (max) precision book
        → pin true_mid = (best_bid + best_ask) / 2

Step 2  Try to fill at default precision
        → Fully filled? Done ✅

Step 3  Fallback to nSigFigs=4 (aggregated, deeper)
        → Inject true_mid from Step 1 as slippage anchor
        → Still not filled? Report as PARTIAL 🟡  (never hidden)
```

`true_mid` is always from the granular book to prevent distortion from aggregated bucket widths.

---

#### Ostium — Dynamic Spread

Oracle-based. No real orderbook; spread computed via Solidity-compatible Pade approximation:

```
spread_bps = (market_spread / 2) + (decayed_OI + trade_size / 2) × priceImpactK / 1e27 × 10,000
```

- `market_spread` = `(ask − bid) / mid` from oracle feed
- `decayed_OI` = existing OI decayed with Pade approximation over elapsed time
- `priceImpactK` = per-pair constant (from `pairs` API)
- Computed separately for buy & sell, then averaged

#### Avantis — Dynamic Spread

Queries a dedicated risk API per direction:

```
GET https://risk-api.avantisfi.com/spread/dynamic
    ?pairIndex=<id>&positionSizeUsdc=<size_wei>&isLong=true&isPnl=false

spread_bps = spreadP / 1e10 × 100
```

Falls back to static `spreadP` from socket API if dynamic is unavailable.

---

### 2. Trading Fees

#### Hyperliquid

Three public API calls, no hardcoded values:

```
scale_if_hip3 = deployerFeeScale + 1       (if deployerFeeScale < 1)
              = deployerFeeScale × 2       (if deployerFeeScale ≥ 1)

growth_scale  = 0.1   (growthMode enabled — 90% discount)
              = 1.0   (growthMode disabled)

taker_fee_bps = userCrossRate × 100 × scale_if_hip3 × growth_scale × 100
maker_fee_bps = userAddRate   × 100 × scale_if_hip3 × growth_scale × 100
```

#### Ostium

From `pairs` API (`takerFeeP / 10,000`). Season overrides applied from `seasons/current` if a `newFee` exists for the asset.

#### Avantis — Skew-Adjusted Opening Fee

Opening fee varies by long/short OI imbalance:

```
open_interest_pct = floor(100 × opposite_OI / (own_OI + position_size + opposite_OI))
pct_index         = min(floor(open_interest_pct / 10), len(skewEqParams) − 1)
open_fee_bps      = ((param1 × open_interest_pct + param2) / 10,000) × 100
```

Closing fee is flat: `closeFeeP × 100`.

---

### 3. Max Leverage

| Exchange | Source |
|---|---|
| Hyperliquid | `metaAndAssetCtxs` → `maxLeverage` per asset |
| Lighter | `10000 / min_initial_margin_fraction` |
| Aster | Max key in `leverageOiRemainingMap` |
| Extended | `tradingConfig.maxLeverage` from `/info/markets` |
| Ostium | `pairs` API → `maxLeverage` ÷ 100 |
| Avantis | `pairInfos[idx].leverages.maxLeverage` |

---

### 4. Funding / Margin Fee

| Exchange | Period | Formula |
|---|---|---|
| Hyperliquid | 1H | `funding_raw × 100` → pct; 24H = 1H × 24 |
| Lighter | 8H | `rate × 100` = 8H pct; 1H = ÷8; 24H = ×3 |
| Aster | 4H | `rate × 100` = 4H pct; 1H = ÷4; 24H = ×6 |
| Extended | 1H | `fundingRate × 100`; 24H = 1H × 24 |
| Ostium | Per-block | `rolloverFeePerBlock × blocks_per_day / 1e18 × 100` (live Arbitrum RPC) |
| Avantis | 1H | `marginFee.long / .short` direct from socket API; 24H = 1H × 24 |

Positive = position pays; Negative = position receives.

---

### 5. Total Cost & Winner

```
effective_spread = slippage_bps × 2        ← orderbook exchanges (round-trip)
                 = slippage_bps            ← Avantis (one-way spread quoted)

total_cost_bps   = effective_spread + open_fee_bps + close_fee_bps
```

**Winner** = exchange with lowest `total_cost_bps` for a fully-filled order.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/assets` | List supported assets + exchange availability |
| `POST` | `/api/compare` | Compare all exchanges (body: `asset`, `order_size`, `order_type`, `direction`) |
| `GET` | `/api/compare/<asset>` | Same via query params: `?size=1000000&order_type=taker&direction=long` |
| `WS` | `compare` event | WebSocket equivalent of POST compare |

```bash
# Example
curl "http://localhost:5001/api/compare/USDJPY?size=1000000&order_type=taker&direction=long"
```

---

## Running Locally

```bash
pip install -r requirements.txt
python app.py          # starts on port 5001 (override: PORT env var)
```

## Deploying to Railway

1. Push to GitHub
2. **New Project** → Deploy from GitHub repo
3. Railway auto-detects `requirements.txt` + `Procfile`
4. Add secrets in Railway **Variables** tab (copy from `.env`)
5. Use generated public URL in your frontend

> `Procfile`: `web: gunicorn -k eventlet -w 1 app:app`  
> `eventlet` is required for WebSocket (SocketIO) support in production.

## Environment Variables

| Variable | Used by |
|---|---|
| `ASTER_API_KEY` | Aster fee fetching |
| `ASTER_SECRET_KEY` | Aster fee fetching |
| `EXTENDED_API_KEY` | Extended orderbook + fees |
| `ONDO_API_KEY` | Ondo Perps markets (fees + leverage) + depth |
| `PORT` | Server port (default `5001`) |
