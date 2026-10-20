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
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv

from models import StandardizedOrderbook, ExecutionCalculator, floor_leverage

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
        # Last fee-load outcome for API debugging (no secrets)
        self._fee_status: Dict[str, Any] = {"code": "pending", "message": "Fee load not finished yet."}
        # Last orderbook failure (per fetch)
        self._last_orderbook_debug: Dict[str, Any] = {}
        # Guards against concurrent/duplicate re-auth attempts (background
        # retry thread + on-demand retry from calculate_execution_cost)
        self._auth_lock = threading.Lock()
        self._last_auth_attempt_ts: float = 0.0
        self._AUTH_RETRY_COOLDOWN_SEC = 30

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
    @staticmethod
    def _gravity_token_from_response(resp: requests.Response) -> Optional[str]:
        token = resp.cookies.get("gravity")
        if token:
            return token
        for header, value in resp.headers.items():
            if header.lower() != "set-cookie" or "gravity=" not in value:
                continue
            part = next((p for p in value.split(";") if p.strip().startswith("gravity=")), None)
            if part:
                return part.strip().split("=", 1)[1]
        return None

    def _record_auth_failure(self, resp: requests.Response, gravity_token, account_id) -> None:
        content_type = resp.headers.get("Content-Type", "")
        server = resp.headers.get("Server", "")
        body_snippet = (resp.text or "")[:300]
        log.warning(
            "GRVT auth failed: gravity=%s account_id=%s status=%s "
            "content_type=%r server=%r body=%r",
            "set" if gravity_token else "missing",
            "set" if account_id else "missing",
            resp.status_code,
            content_type,
            server,
            body_snippet,
        )
        self._fee_status = {
            "code":         "auth_failed",
            "http_status":  resp.status_code,
            "content_type": content_type,
            "server":       server,
            "gravity":      "set" if gravity_token else "missing",
            "account_id":   "set" if account_id else "missing",
            "body_snippet": body_snippet,
            "message":      "Login did not return gravity session cookie or X-Grvt-Account-Id.",
        }

    def _apply_fee_tier(self, fee_resp: requests.Response) -> None:
        if fee_resp.status_code != 200:
            body_snippet = (fee_resp.text or "")[:300]
            log.warning("GRVT fee fetch failed: status=%s body=%r", fee_resp.status_code, body_snippet)
            self._fee_status = {
                "code":         "fee_fetch_failed",
                "http_status":  fee_resp.status_code,
                "body_snippet": body_snippet,
                "message":      "funding_account_summary request failed.",
            }
            return
        tier = fee_resp.json().get("t", {})
        ft = tier.get("ft")
        fm = tier.get("fm")
        if ft is not None:
            self._taker_fee_bps = float(ft) / 100
            log.info(
                "GRVT fees loaded: taker=%.2f bps, maker=%.2f bps",
                self._taker_fee_bps, self._maker_fee_bps or 0,
            )
            self._fee_status = {"code": "ok", "message": "Taker/maker fees loaded."}
        else:
            self._fee_status = {
                "code":    "fee_tier_missing_ft",
                "message": "funding_account_summary returned no taker fee (ft) in tier.",
            }
        if fm is not None:
            self._maker_fee_bps = float(fm) / 100

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
            self._fee_status = {
                "code":    "no_api_key",
                "message": "GRVT_API_KEY is not set in the environment.",
            }
            return

        try:
            resp = requests.post(
                self.AUTH_URL,
                json={"api_key": api_key},
                headers={"Cookie": "rm=true;", "Content-Type": "application/json"},
                timeout=15,
            )
            gravity_token = self._gravity_token_from_response(resp)
            account_id = resp.headers.get("x-grvt-account-id") or resp.headers.get("X-Grvt-Account-Id")
            if not gravity_token or not account_id:
                self._record_auth_failure(resp, gravity_token, account_id)
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
            self._apply_fee_tier(fee_resp)
        except (TypeError, ValueError, KeyError, OSError):
            log.exception("GRVT fee auth error")
            self._fee_status = {
                "code":    "auth_exception",
                "message": "Exception during GRVT login or fee fetch; see server logs.",
            }

    def _try_reauth_if_stale(self) -> None:
        """
        Best-effort synchronous re-auth, throttled to once per
        ``_AUTH_RETRY_COOLDOWN_SEC``. Lets a live request self-heal GRVT
        immediately (instead of waiting on the background retry thread's
        next scheduled attempt), without every concurrent request hammering
        the login endpoint.
        """
        now = time.time()
        if now - self._last_auth_attempt_ts < self._AUTH_RETRY_COOLDOWN_SEC:
            return
        if not self._auth_lock.acquire(blocking=False):
            return
        try:
            self._last_auth_attempt_ts = time.time()
            self._authenticate_and_load_fees()
        finally:
            self._auth_lock.release()

    def _start_auth_retry(self) -> None:
        def _retry():
            backoff = [5, 15, 30, 60]
            attempt = 0
            while self._taker_fee_bps is None:
                delay = backoff[min(attempt, len(backoff) - 1)]
                time.sleep(delay)
                attempt += 1
                with self._auth_lock:
                    self._last_auth_attempt_ts = time.time()
                    self._authenticate_and_load_fees()
                if self._taker_fee_bps is not None:
                    log.info("GRVT auth succeeded on retry (attempt=%d, delay=%ds)", attempt, delay)
                    return
                log.warning(
                    "GRVT auth retry %d failed (code=%s) — retrying in %ds",
                    attempt, self._fee_status.get("code"), backoff[min(attempt, len(backoff) - 1)],
                )

        t = threading.Thread(target=_retry, daemon=True, name="grvt-auth-retry")
        t.start()
        log.info("GRVT auth retry scheduled in background (indefinite, capped at 60s cadence)")

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
        except (TypeError, ValueError, KeyError, OSError):
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
            return floor_leverage(self._leverage_cache[instrument])
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
            lev = floor_leverage(brackets[0].get('max_leverage')) if brackets else None
            self._leverage_cache[instrument] = lev
            return self._leverage_cache[instrument]
        except (TypeError, ValueError, KeyError, OSError):
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
                self._last_orderbook_debug = {
                    "code":        "orderbook_http_error",
                    "http_status": resp.status_code,
                    "instrument":  instrument,
                    "message":     "GRVT market-data book request failed.",
                }
                return None
            book = resp.json().get('result', {})
            if not book:
                self._last_orderbook_debug = {
                    "code":       "orderbook_empty_result",
                    "instrument": instrument,
                    "message":    "book response had no result payload.",
                }
                return None

            raw_asks = book.get('asks', [])
            raw_bids = book.get('bids', [])
            if not raw_asks or not raw_bids:
                self._last_orderbook_debug = {
                    "code":       "orderbook_no_sides",
                    "instrument": instrument,
                    "message":    "book had missing bids or asks.",
                }
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
        except (TypeError, ValueError, KeyError, OSError):
            log.exception("GRVT orderbook error for %s", instrument)
            self._last_orderbook_debug = {
                "code":       "orderbook_exception",
                "instrument": instrument,
                "message":    "Exception fetching book; see server logs.",
            }
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
    ) -> Tuple[Optional[Dict], Optional[Dict[str, Any]]]:
        """
        Calculate the round-trip execution cost for a given order size.

        Slippage is measured from mid-price ((best_bid + best_ask) / 2),
        consistent with all other orderbook-based venues.

        For a long:  opening = buy slippage,  closing = sell slippage.
        For a short: opening = sell slippage, closing = buy slippage.

        Returns:
            (result_dict_or_none, debug_dict_or_none). When the first value is None,
            the second contains a stable ``code`` for operators (no secrets).
        """
        if not os.getenv("GRVT_API_KEY"):
            return None, {
                "code":    "no_api_key",
                "message": "GRVT_API_KEY is not set in the environment.",
            }

        if self._taker_fee_bps is None:
            self._try_reauth_if_stale()

        if self._taker_fee_bps is None:
            dbg = dict(self._fee_status) if self._fee_status else {"code": "fees_unknown"}
            dbg.setdefault(
                "message",
                "Taker fee not loaded; check GRVT_API_KEY, auth, and funding_account_summary.",
            )
            return None, dbg

        ob = self.get_orderbook(instrument)
        if not ob:
            return None, self._last_orderbook_debug or {
                "code":       "orderbook_unavailable",
                "instrument": instrument,
                "message":    "Orderbook fetch returned no usable book.",
            }

        taker_fee = self._taker_fee_bps

        result = ExecutionCalculator.calculate_execution_cost(
            ob, order_size_usd, taker_fee, taker_fee
        )
        if not result:
            return None, {
                "code":       "execution_walk_failed",
                "instrument": instrument,
                "message":    "Could not fill order against book (insufficient depth or empty walk).",
            }

        buy_slip_bps  = result.get('buy_slippage_bps', 0.0)
        sell_slip_bps = result.get('sell_slippage_bps', 0.0)

        is_long = direction.lower() == 'long'
        result['opening_slippage_bps'] = buy_slip_bps  if is_long else sell_slip_bps
        result['closing_slippage_bps'] = sell_slip_bps if is_long else buy_slip_bps
        result['slippage_type']        = 'opening_closing'
        result['total_cost_bps']       = buy_slip_bps + sell_slip_bps + taker_fee * 2
        result['symbol']               = symbol or instrument

        result['max_leverage']  = self.get_max_leverage(instrument)
        result['taker_fee_bps'] = self._taker_fee_bps
        result['maker_fee_bps'] = self._maker_fee_bps
        return result, None
