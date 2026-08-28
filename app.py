#!/usr/bin/env python3
"""
app.py - Flask application, REST routes, and WebSocket handlers.

Entry point for the Multi-Exchange Slippage Comparison API.
Run with:  python app.py
"""
from __future__ import annotations

import eventlet
import sys
if sys.platform == 'darwin':
    import eventlet.hubs
    eventlet.hubs.use_hub('poll')  # kqueue is broken on macOS Python 3.9+
eventlet.monkey_patch()

import logging
import math
import os
import secrets

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit

from models import ASSETS
from comparator import FeeComparator
from exchanges.variational import VariationalAPI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
MAX_ORDER_SIZE = 1_000_000_000  # $1B upper bound
MIN_ORDER_SIZE = 1              # $1 lower bound
VALID_ORDER_TYPES = {'taker', 'maker'}
VALID_DIRECTIONS = {'long', 'short'}
COMPARE_FAILED = {'error': 'Failed to compare asset'}


def _validated_order_size(raw) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    if val < MIN_ORDER_SIZE or val > MAX_ORDER_SIZE:
        return None
    return val


def _bad_request(message: str, extra: dict = None):
    payload = {'error': message}
    if extra:
        payload.update(extra)
    return jsonify(payload), 400


def _execute_compare(asset: str, order_size: float, order_type: str, direction: str):
    result = comparator.compare_asset(asset, order_size, order_type=order_type, direction=direction)
    if not result:
        return jsonify(COMPARE_FAILED), 500
    try:
        result = comparator.calculate_totals_and_winner(result, asset, order_type, direction)
        return jsonify(result)
    except (TypeError, ValueError, KeyError):
        log.exception("compare failed for %s", asset)
        return jsonify(COMPARE_FAILED), 500


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*")
CORS(app, origins=allowed_origins)
socketio = SocketIO(app, cors_allowed_origins=allowed_origins, async_mode='eventlet')

# Initialize comparator (loads all exchange caches on startup)
comparator = FeeComparator()


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------
@app.route('/api/assets', methods=['GET'])
def get_assets():
    """Return list of available assets."""
    assets_list = []
    for key, config in ASSETS.items():
        assets_list.append({
            'key':    key,
            'name':   config.name,
            'symbol': config.symbol_key,
            'exchanges': {
                'hyperliquid':  config.hyperliquid_symbol is not None,
                'lighter':      config.lighter_market_id  is not None,
                'aster':        config.aster_symbol       is not None,
                'avantis':      True,
                'ostium':       config.ostium_symbol      is not None,
                'extended':     config.extended_symbol    is not None,
                'edgex':        config.edgex_contract_id  is not None,
                'grvt':         config.grvt_instrument    is not None,
                'variational':  key.upper() in VariationalAPI.VARIATIONAL_SYMBOLS,
                'ondo':         config.ondo_symbol        is not None,
                'pacifica':     config.pacifica_symbol    is not None,
            },
        })
    return jsonify({'assets': assets_list})


@app.route('/api/compare', methods=['POST'])
def compare():
    """Compare slippage across exchanges for given asset and order size."""
    data = request.get_json(silent=True)
    if not data:
        return _bad_request('Request body must be valid JSON')

    asset      = str(data.get('asset', '')).upper()
    order_size = _validated_order_size(data.get('order_size', 1_000_000))
    order_type = str(data.get('order_type', 'taker')).lower()
    direction  = str(data.get('direction',  'long')).lower()

    if order_size is None:
        return _bad_request(f'order_size must be a number between {MIN_ORDER_SIZE} and {MAX_ORDER_SIZE}')
    if order_type not in VALID_ORDER_TYPES:
        return _bad_request(f'order_type must be one of: {", ".join(VALID_ORDER_TYPES)}')
    if direction not in VALID_DIRECTIONS:
        return _bad_request(f'direction must be one of: {", ".join(VALID_DIRECTIONS)}')
    if asset not in ASSETS:
        return _bad_request(f'Asset {asset} not found')

    return _execute_compare(asset, order_size, order_type, direction)


@app.route('/api/compare/<asset>', methods=['GET'])
def compare_get(asset: str):
    """GET endpoint for comparing slippage across exchanges."""
    asset      = asset.upper()
    order_size = _validated_order_size(request.args.get('size', 1_000_000))
    order_type = request.args.get('order_type', 'taker').lower()
    direction  = request.args.get('direction',  'long').lower()

    if order_size is None:
        return _bad_request(f'size must be a number between {MIN_ORDER_SIZE} and {MAX_ORDER_SIZE}')
    if order_type not in VALID_ORDER_TYPES:
        return _bad_request(f'order_type must be one of: {", ".join(VALID_ORDER_TYPES)}')
    if direction not in VALID_DIRECTIONS:
        return _bad_request(f'direction must be one of: {", ".join(VALID_DIRECTIONS)}')
    if asset not in ASSETS:
        return _bad_request(f'Asset {asset} not found', {'available_assets': list(ASSETS.keys())})

    return _execute_compare(asset, order_size, order_type, direction)


# ---------------------------------------------------------------------------
# WebSocket handlers
# ---------------------------------------------------------------------------
@socketio.on('compare')
def handle_compare(data):
    """Handle WebSocket compare request."""
    try:
        if not isinstance(data, dict):
            emit('compare_error', {'error': 'Invalid request format'})
            return

        asset      = str(data.get('asset', '')).upper()
        order_size = _validated_order_size(data.get('order_size', 1_000_000))
        order_type = str(data.get('order_type', 'taker')).lower()
        direction  = str(data.get('direction',  'long')).lower()

        if order_size is None:
            emit('compare_error', {'error': f'order_size must be between {MIN_ORDER_SIZE} and {MAX_ORDER_SIZE}'})
            return
        if order_type not in VALID_ORDER_TYPES:
            emit('compare_error', {'error': f'order_type must be one of: {", ".join(VALID_ORDER_TYPES)}'})
            return
        if direction not in VALID_DIRECTIONS:
            emit('compare_error', {'error': f'direction must be one of: {", ".join(VALID_DIRECTIONS)}'})
            return
        if not asset or asset not in ASSETS:
            emit('compare_error', {'error': f'Unknown asset: {asset}'})
            return

        result = comparator.compare_asset(asset, order_size, order_type=order_type, direction=direction)
        if not result:
            emit('compare_error', COMPARE_FAILED)
            return

        result = comparator.calculate_totals_and_winner(result, asset, order_type, direction)
        emit('compare_result', result)
    except (TypeError, ValueError, KeyError):
        log.exception("WebSocket compare handler error")
        emit('compare_error', {'error': 'Internal server error'})


# ---------------------------------------------------------------------------
# Health check for K8s probes (CD_SETUP still hits `/`)
# ---------------------------------------------------------------------------
@app.route('/')
@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    port  = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")

    log.info("=" * 60)
    log.info("FIXED FEE & AVERAGE SLIPPAGE COMPARISON API SERVER")
    log.info("=" * 60)
    log.info("Running on port %d (debug=%s)", port, debug)
    log.info("WebSocket support enabled")
    log.info("=" * 60)

    socketio.run(app, host="0.0.0.0", debug=debug, port=port, use_reloader=False)
