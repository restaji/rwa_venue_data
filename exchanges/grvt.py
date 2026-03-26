#!/usr/bin/env python3
"""
exchanges/grvt.py - GRVT exchange integration.

Covers: orderbook-based slippage, taker/maker fees (from authenticated
funding_account_summary), max leverage (tier-1 from public margin_rules),
and funding rate (converted to 24H).

Public APIs used:
  Instruments: POST https://market-data.grvt.io/full/v1/all_instruments
  Margin:      POST https://market-data.grvt.io/full/v1/margin_rules
  Orderbook:   POST https://market-data.grvt.io/full/v1/book
  Funding:     POST https://market-data.grvt.io/full/v1/funding

Authenticated APIs (requires GRVT_API_KEY in .env):
  Login:       POST https://edge.grvt.io/auth/api_key/login
  Fees:        POST https://trades.grvt.io/lite/v1/funding_account_summary
               → t.ft / 100 = taker_fee_bps, t.fm / 100 = maker_fee_bps

Instrument naming convention: {BASE}_USDT_Perp  (e.g. XAU_USDT_Perp)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

import requests
from dotenv import load_dotenv

from models import StandardizedOrderbook, ExecutionCalculator

load_dotenv()
log = logging.getLogger(__name__)


class GRVTAPI:
    """Client for GRVT exchange orderbook, fee, and funding data."""

    BASE_URL   = "https://market-data.grvt.io/full/v1"
    AUTH_URL   = "https://edge.grvt.io/auth/api_key/login"
    TRADES_URL = "https://trades.grvt.io/lite/v1"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept":       "application/json",
        })
        # instrument name -> {funding_interval_hours}
        self._instrument_meta: Dict[str, Dict] = {}
        # instrument name -> max_leverage (cached after first fetch)
        self._leverage_cache: Dict[str, Optional[float]] = {}
        # fees loaded from funding_account_summary (None until auth succeeds)
        self._taker_fee_bps: Optional[float] = None
        self._maker_fee_bps: Optional[float] = None
        # authenticated session state
        self._account_id: Optional[str] = None

        self._load_instrument_meta()
        self._authenticate_and_load_fees()
        if self._taker_fee_bps is None:
            self._start_auth_retry()

    @property
    def is_ready(self) -> bool:
        """True once fees are loaded, or if no API key is configured (intentionally disabled)."""
        if not os.environ.get('GRVT_API_KEY'):
            return True
        return self._taker_fee_bps is not None

    # ------------------------------------------------------------------
    # Startup: instrument meta + auth + fees
    # ------------------------------------------------------------------
    def _authenticate_and_load_fees(self) -> None:
        """
        Login with GRVT_API_KEY from .env, then fetch real taker/maker fees
        from funding_account_summary.

        Response field units: ft/fm are in centibeeps (0.01 bps each).
        e.g. ft=450 → 450/100 = 4.5 bps taker, fm=-1 → -0.01 bps maker.
        """
        api_key = os.getenv("GRVT_API_KEY")
        if not api_key:
            log.warning("GRVT_API_KEY not configured")
            return

        try:
            resp = requests.post(
                self.AUTH_URL,
                json={"api_key": api_key},
                headers={"Cookie": "rm=true;", "Content-Type": "application/json"},
                timeout=15,
            )

            gravity_token = resp.cookies.get("gravity")
            if not gravity_token:
                for header, value in resp.headers.items():
                    if header.lower() == "set-cookie" and "gravity=" in value:
                        part = next(
                            (p for p in value.split(";") if p.strip().startswith("gravity=")),
                            None,
                        )
                        if part:
                            gravity_token = part.strip().split("=", 1)[1]
                        break

            account_id = resp.headers.get("x-grvt-account-id") or resp.headers.get("X-Grvt-Account-Id")

            if not gravity_token or not account_id:
                log.warning("GRVT auth failed: gravity=%r account_id=%r status=%s", gravity_token, account_id, resp.status_code)
                return

            self._account_id = account_id

            fee_resp = requests.post(
                f"{self.TRADES_URL}/funding_account_summary",
                json={},
                headers={
                    "Cookie":            f"gravity={gravity_token}",
                    "X-Grvt-Account-Id": account_id,
                    "Content-Type":      "application/json",
                },
                timeout=15,
            )
            if fee_resp.status_code != 200:
                log.warning("GRVT fee fetch failed: status=%s", fee_resp.status_code)
                return

            tier = fee_resp.json().get("t", {})
            ft = tier.get("ft")
            fm = tier.get("fm")

            if ft is not None:
                self._taker_fee_bps = float(ft) / 100
                log.info("GRVT fees loaded: taker=%.2f bps, maker=%.2f bps", self._taker_fee_bps, self._maker_fee_bps or 0)
            if fm is not None:
                self._maker_fee_bps = float(fm) / 100

        except Exception:
            log.exception("GRVT fee auth error")

    def _start_auth_retry(self) -> None:
        """Retry authentication in a background thread after startup failure."""
        def _retry():
            for delay in [5, 15, 30, 60]:
                time.sleep(delay)
                self._authenticate_and_load_fees()
                if self._taker_fee_bps is not None:
                    log.info("GRVT auth succeeded on retry (delay=%ds)", delay)
                    return
            log.warning("GRVT auth failed after all retries — GRVT results will be skipped")

        t = threading.Thread(target=_retry, daemon=True, name="grvt-auth-retry")
        t.start()
        log.info("GRVT auth retry scheduled in background")

    def _load_instrument_meta(self) -> None:
        """Load funding_interval_hours for all active perpetuals at startup."""
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/all_instruments",
                json={"kind": ["PERPETUAL"], "underlying": [], "quote": ["USDT"], "is_active": True},
                timeout=30,
            )
            if resp.status_code != 200:
                return
            for inst in resp.json().get('result', []):
                name = inst.get('instrument')
                if name:
                    self._instrument_meta[name] = {
                        'funding_interval_hours': inst.get('funding_interval_hours', 8),
                    }
        except Exception:
            log.exception("GRVT instrument meta load error")

    # ------------------------------------------------------------------
    # Fees
    # ------------------------------------------------------------------
    def get_fees(self, instrument: str) -> Tuple[Optional[float], Optional[float]]:
        """Return (taker_fee_bps, maker_fee_bps), or (None, None) if not loaded."""
        if instrument and instrument in self._instrument_meta:
            return self._taker_fee_bps, self._maker_fee_bps
        return None, None

    # ------------------------------------------------------------------
    # Max leverage
    # ------------------------------------------------------------------
    def get_max_leverage(self, instrument: str) -> Optional[float]:
        """
        Return tier-1 (smallest notional = highest leverage) max_leverage
        from the public margin_rules endpoint. Result is cached after first call.
        """
        if instrument in self._leverage_cache:
            return self._leverage_cache[instrument]
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/margin_rules",
                json={"instrument": instrument},
                timeout=15,
            )
            if resp.status_code != 200:
                self._leverage_cache[instrument] = None
                return None
            brackets = resp.json().get('risk_brackets', [])
            lev = float(brackets[0].get('max_leverage', 0)) if brackets else None
            self._leverage_cache[instrument] = lev or None
            return self._leverage_cache[instrument]
        except Exception:
            log.exception("GRVT max leverage error for %s", instrument)
            self._leverage_cache[instrument] = None
            return None

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------
    def get_orderbook(self, instrument: str, depth: int = 500) -> Optional[StandardizedOrderbook]:
        """
        Fetch the orderbook and return a StandardizedOrderbook.
        Defaults to 500 (maximum).
        """
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/book",
                json={"instrument": instrument, "depth": depth},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            book = resp.json().get('result', {})
            if not book:
                return None

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
            log.exception("GRVT orderbook error for %s", instrument)
            return None

    # ------------------------------------------------------------------
    # Execution cost
    # ------------------------------------------------------------------
    def calculate_execution_cost(
        self,
        instrument: str,
        order_size_usd: float,
        direction: str = 'long',
        symbol: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Calculate the round-trip execution cost for a given order size.

        Slippage is measured from best_ask (buy) / best_bid (sell), matching
        the price-impact convention used by GRVT's UI.

        For a long:  opening = buy slippage,  closing = sell slippage.
        For a short: opening = sell slippage, closing = buy slippage.
        """
        if self._taker_fee_bps is None:
            return None

        ob = self.get_orderbook(instrument)
        if not ob:
            return None

        taker_fee = self._taker_fee_bps

        result = ExecutionCalculator.calculate_execution_cost(
            ob, order_size_usd, taker_fee, taker_fee
        )
        if not result:
            return None

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
        result['total_cost_bps']       = buy_slip_bps + sell_slip_bps + taker_fee * 2
        result['symbol']               = symbol or instrument

        result['max_leverage']  = self.get_max_leverage(instrument)
        result['taker_fee_bps'] = self._taker_fee_bps
        result['maker_fee_bps'] = self._maker_fee_bps
        return result
