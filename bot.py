# File: bingx_http_trading_bot.py

import time
import pandas as pd
import numpy as np
import datetime
import requests
from api import API_SECRET, API_KEY
import hmac
from hashlib import sha256
import uuid

# ================== CONFIG ===================

SYMBOLS = ['ETH-USDT']
TIMEFRAME = '15m'
TRADE_QTY = 0.01  # Fixed manual quantity, e.g., 0.01 ETH
SL_AMOUNT = 0.80  # Stop loss in USDT
TP_AMOUNT = 0.50  # Take profit in USDT

APIURL = "https://open-api.bingx.com"
HEADERS = {
    'X-BX-APIKEY': API_KEY
}

# ================== SIGNED REQUEST UTILS ===============
def get_timestamp():
    return str(int(time.time() * 1000))

def get_sign(secret, param_str):
    return hmac.new(secret.encode(), param_str.encode(), sha256).hexdigest()

def parse_param(params: dict):
    return '&'.join(f"{k}={params[k]}" for k in sorted(params))

def send_signed_request(method, path, base_params=None):
    if base_params is None:
        base_params = {}
    base_params["timestamp"] = get_timestamp()
    base_params["recvWindow"] = "60000"
    param_str = parse_param(base_params)
    signature = get_sign(API_SECRET, param_str)
    full_url = f"{APIURL}{path}?{param_str}&signature={signature}"
    response = requests.request(method, full_url, headers=HEADERS)
    j = response.json()
    if response.status_code != 200 or j.get("code") != 0:
        raise ValueError(f"API Error: {j}")
    return j

# ================== DATA FETCH ================
def fetch_ohlcv(symbol, interval, limit=150):
    print(f"📈 Fetching OHLCV for {symbol}...")
    r = requests.get(f"{APIURL}/openApi/swap/v2/quote/klines", params={
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    })
    j = r.json()
    if r.status_code != 200 or j.get("code") != 0 or 'data' not in j:
        raise ValueError(f"OHLCV fetch error: {j}")
    data = j['data']
    if isinstance(data, dict):
        raise ValueError(f"Unexpected OHLCV data format: {data}")
    df = pd.DataFrame(data)
    if df.shape[1] != 6:
        raise ValueError("Unexpected number of columns in OHLCV response")
    df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    df['timestamp'] = pd.to_datetime(pd.to_numeric(df['timestamp'], errors='coerce'), unit='ms')
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df

# ================== REST API ==================
def get_balance():
    j = send_signed_request('GET', '/openApi/swap/v2/user/balance')
    data = j.get('data', [])
    if isinstance(data, list):
        for asset in data:
            if asset.get('asset') == 'USDT':
                return float(asset.get('availableBalance', 0))
        raise ValueError("USDT balance not found in balance list")
    elif isinstance(data, dict):
        print(f"[DEBUG] Balance response dict: {data}")
        return float(data.get('availableBalance', 0))
    else:
        raise ValueError("Unexpected balance data structure")

def generate_client_order_id():
    return "ccbot-" + uuid.uuid4().hex[:16]

def place_order(symbol, side, qty, entry_price):
    position_side = 'LONG' if side.lower() == 'buy' else 'SHORT'
    print(f"🛒 Placing {side.upper()} order on {symbol} for qty: {qty}...")

    order = send_signed_request('POST', '/openApi/swap/v2/trade/order', {
        'symbol': symbol,
        'side': side.upper(),
        'type': 'MARKET',
        'positionSide': position_side,
        'quantity': qty,
        'clientOrderID': generate_client_order_id()
    })

    sl_price = entry_price - SL_AMOUNT if side == 'buy' else entry_price + SL_AMOUNT
    tp_price = entry_price + TP_AMOUNT if side == 'buy' else entry_price - TP_AMOUNT

    send_signed_request('POST', '/openApi/swap/v2/trade/order', {
        'symbol': symbol,
        'side': 'SELL' if side == 'buy' else 'BUY',
        'type': 'TAKE_PROFIT_MARKET',
        'stopPrice': round(tp_price, 2),
        'closePosition': True,
        'clientOrderID': generate_client_order_id(),
        'workingType': 'MARK_PRICE'
    })

    send_signed_request('POST', '/openApi/swap/v2/trade/order', {
        'symbol': symbol,
        'side': 'SELL' if side == 'buy' else 'BUY',
        'type': 'STOP_MARKET',
        'stopPrice': round(sl_price, 2),
        'closePosition': True,
        'clientOrderID': generate_client_order_id(),
        'workingType': 'MARK_PRICE'
    })

    return order

def in_position(symbol):
    j = send_signed_request('GET', '/openApi/swap/v2/user/positions', {"symbol": symbol})
    if not j['data']:
        return False
    for p in j['data']:
        if float(p.get('positionAmt', 0)) != 0:
            return True
    return False

# ================== STRATEGY ==================
def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    vwap = (tp * df['volume']).cumsum() / df['volume'].cumsum()
    return vwap

def trade_logic(symbol):
    print(f"🔍 Analyzing {symbol}...")
    if in_position(symbol):
        print(f"⛔ Already in position for {symbol}")
        return

    df = fetch_ohlcv(symbol, TIMEFRAME)
    df['vwap'] = compute_vwap(df)
    df['ema_20'] = compute_ema(df['close'], 20)

    last = df.iloc[-1]
    price = last['close']
    vwap = last['vwap']
    ema = last['ema_20']

    print(f"📊 Price: {price}, VWAP: {vwap:.2f}, EMA20: {ema:.2f}")
    print(get_balance())

    if price > vwap and price > ema:
        place_order(symbol, 'buy', TRADE_QTY, price)
        print(f"✅ LONG {symbol}")

    elif price < vwap and price < ema:
        place_order(symbol, 'sell', TRADE_QTY, price)
        print(f"✅ SHORT {symbol}")
    else:
        print(f"⏸️ No trade condition met for {symbol}")

# ================== MAIN =====================
if __name__ == '__main__':
    print("🚀 Trading bot started...")
    while True:
        for symbol in SYMBOLS:
            try:
                trade_logic(symbol)
            except Exception as e:
                print(f"[Unhandled Error] {e}")
        time.sleep(60)
