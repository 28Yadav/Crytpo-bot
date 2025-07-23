# File: trading_bot_macd_adx.py

import time
import pandas as pd
import numpy as np
import datetime
import ccxt
import uuid
from decimal import Decimal, getcontext

getcontext().prec = 18

# ================== CONFIG ===================

SYMBOLS = ['ETH/USDT:USDT', 'BTC/USDT:USDT']
TIMEFRAME = '15m'
ORDER_SIZE_BY_SYMBOL = {
    'ETH/USDT:USDT': Decimal('0.04'),
    'BTC/USDT:USDT': Decimal('0.001')
}
TP_PERCENT = Decimal('0.02')
SL_PERCENT = Decimal('0.08') 
COOLDOWN_PERIOD = 60 * 30

exchange = ccxt.bingx({
    'apiKey': "wGY6iowJ9qdr1idLbKOj81EGhhZe5O8dqqZlyBiSjiEZnuZUDULsAW30m4eFaZOu35n5zQktN7a01wKoeSg",
    'secret': "tqxcIVDdDJm2GWjinyBJH4EbvJrjIuOVyi7mnKOzhXHquFPNcULqMAOvmSy0pyuoPOAyCzE2zudzEmlwnA",
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
    }
})

last_trade_time = {symbol: 0 for symbol in SYMBOLS}

# ================== DATA FETCH ================
def fetch_ohlcv(symbol, timeframe, limit=150):
    print(f"📈 Fetching OHLCV for {symbol}...")
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def get_balance():
    balance = exchange.fetch_balance({'type': 'swap'})
    usdt = balance.get('free', {}).get('USDT', 0)
    print(f"[DEBUG] USDT Free Balance: {usdt}")
    return Decimal(str(usdt))

def generate_client_order_id():
    return "ccbot-" + uuid.uuid4().hex[:16]

# ================== ORDER EXECUTION =======================# 
def place_order(symbol, side, entry_price):
    print(f"🛒 Placing {side.upper()} order on {symbol}...")
    try:
        entry_price = float(entry_price)
        qty = float(ORDER_SIZE_BY_SYMBOL.get(symbol, Decimal('0')))
    except Exception as e:
        print(f"[Qty Error] {e}")
        return

    print(f"[DEBUG] Qty: {qty}")

    try:
        exchange.set_position_mode(True)
    except Exception as e:
        print(f"[Mode Error] {e}")
        return

    try:
        leverage_side = 'LONG' if side == 'buy' else 'SHORT'
        exchange.set_leverage(15, symbol, params={'side': leverage_side})
    except Exception as e:
        print(f"[Leverage Error] {e}")
        return

    order_params = {
        'marginMode': 'cross',
        'positionSide': leverage_side,
        'type': 'swap',
        'clientOrderId': generate_client_order_id()
    }

    try:
        order = exchange.create_order(symbol, 'market', side, qty, None, order_params)
    except ccxt.InsufficientFunds as e:
        print(f"[FAILURE] Order rejected: {str(e)}")
        return

    sl_price = round(entry_price * (1 - float(SL_PERCENT)) if side == 'buy' else entry_price * (1 + float(SL_PERCENT)), 2)
    tp_price = round(entry_price * (1 + float(TP_PERCENT)) if side == 'buy' else entry_price * (1 - float(TP_PERCENT)), 2)

    try:
        exchange.create_order(symbol, 'STOP_MARKET', 'sell' if side == 'buy' else 'buy', qty, 0.0, {
            'stopPrice': sl_price,
            'marginMode': 'cross',
            'positionSide': leverage_side
        })
    except Exception as e:
        print(f"[SL Error] {e}")

    try:
        exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', 'sell' if side == 'buy' else 'buy', qty, 0.0, {
            'stopPrice': tp_price,
            'marginMode': 'cross',
            'positionSide': leverage_side
        })
    except Exception as e:
        print(f"[TP Error] {e}")

    last_trade_time[symbol] = time.time()
    return order

def in_position(symbol):
    positions = exchange.fetch_positions([symbol])
    for pos in positions:
        if float(pos.get('contracts', 0)) != 0:
            return True
    return False

# ================== STRATEGY ==================
def compute_macd(df):
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line

def compute_adx(df, period=14):
    delta_high = df['high'].diff()
    delta_low = df['low'].diff()
    plus_dm = np.where((delta_high > delta_low) & (delta_high > 0), delta_high, 0)
    minus_dm = np.where((delta_low > delta_high) & (delta_low > 0), delta_low, 0)
    tr1 = df['high'] - df['low']
    tr2 = abs(df['high'] - df['close'].shift())
    tr3 = abs(df['low'] - df['close'].shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (pd.Series(plus_dm).rolling(window=period).sum() / atr)
    minus_di = 100 * (pd.Series(minus_dm).rolling(window=period).sum() / atr)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    adx = dx.rolling(window=period).mean()
    return adx, plus_di, minus_di

def is_fresh_signal(df):
    if len(df) < 3:
        return False
    macd_line, signal_line = compute_macd(df)
    adx, plus_di, minus_di = compute_adx(df)
    prev_macd_cross = macd_line.iloc[-3] < signal_line.iloc[-3] and macd_line.iloc[-2] > signal_line.iloc[-2]
    prev_macd_cross_down = macd_line.iloc[-3] > signal_line.iloc[-3] and macd_line.iloc[-2] < signal_line.iloc[-2]
    fresh_adx_up = adx.iloc[-2] < 25 and adx.iloc[-1] >= 25
    if (prev_macd_cross and plus_di.iloc[-1] > minus_di.iloc[-1] and fresh_adx_up):
        return 'buy'
    elif (prev_macd_cross_down and minus_di.iloc[-1] > plus_di.iloc[-1] and fresh_adx_up):
        return 'sell'
    return None

def trade_logic(symbol):
    print(f"🔍 Analyzing {symbol}...")
    if in_position(symbol):
        print(f"⛔️ Already in position for {symbol}")
        return False

    if symbol in last_trade_time:
        since_last = time.time() - last_trade_time[symbol]
        if since_last < COOLDOWN_PERIOD:
            print(f"⏳ Cooling down ({int((COOLDOWN_PERIOD - since_last) / 60)} min left)...")
            return False

    df = fetch_ohlcv(symbol, TIMEFRAME)
    signal = is_fresh_signal(df)
    if not signal:
        print("⚠️ Signal not fresh. Skipping...")
        return False

    last = df.iloc[-1]
    price = last['close']
    place_order(symbol, signal, price)
    print(f"✅ {signal.upper()} {symbol}")
    return True

# ================== MAIN =====================
if __name__ == '__main__':
    print("🚀 Trading bot started...")
    while True:
        for symbol in SYMBOLS:
            try:
                trade_logic(symbol)
            except Exception as e:
                print(f"[Unhandled Error] {e}")
        print("⏰ Cycle complete, sleeping 60 seconds...")
        time.sleep(60)
