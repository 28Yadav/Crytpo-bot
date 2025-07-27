# FILE: trading_bot.py

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
    'ETH/USDT:USDT': Decimal('0.07'),
    'BTC/USDT:USDT': Decimal('0.0025')
}
TP_PERCENT = Decimal('0.01')
SL_PERCENT = Decimal('0.05') 
COOLDOWN_PERIOD = 60 * 30
FRESH_SIGNAL_MAX_AGE_CANDLES = 1
FRESH_SIGNAL_MAX_PRICE_DEVIATION = 0.004  # 0.5%

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
        exchange.set_leverage(15, symbol, params={'marginMode': 'cross', 'side': leverage_side})
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

    tp_price = round(entry_price * (1 + float(TP_PERCENT)) if side == 'buy' else entry_price * (1 - float(TP_PERCENT)), 2)

    try:
        exchange.create_order(symbol, 'take_profit_market', 'sell' if side == 'buy' else 'buy', qty, None, {
            'triggerPrice': tp_price,
            'stopPrice': tp_price,
            'positionSide': leverage_side,
            'marginMode': 'cross'
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
def compute_supertrend(df, period=10, multiplier=3):
    hl2 = (df['high'] + df['low']) / 2
    atr = df['high'].combine(df['low'], max) - df['low'].combine(df['high'], min)
    atr = atr.rolling(window=period).mean()

    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    supertrend = pd.Series(index=df.index, dtype='float64')
    direction = pd.Series(index=df.index, dtype='bool')

    for i in range(1, len(df)):
        if df['close'][i] > upperband[i - 1]:
            supertrend[i] = lowerband[i]
            direction[i] = True
        elif df['close'][i] < lowerband[i - 1]:
            supertrend[i] = upperband[i]
            direction[i] = False
        else:
            supertrend[i] = supertrend[i - 1]
            direction[i] = direction[i - 1]

    return direction

def compute_stochastic(df, k_period=14, d_period=3):
    low_min = df['low'].rolling(window=k_period).min()
    high_max = df['high'].rolling(window=k_period).max()
    k = 100 * (df['close'] - low_min) / (high_max - low_min)
    d = k.rolling(window=d_period).mean()
    return k, d

def is_fresh_signal(df):
    if len(df) < 20:
        return None

    st_dir = compute_supertrend(df)
    k, d = compute_stochastic(df)

    signal = None
    price = df.iloc[-1]['close']

    cross_up = k.iloc[-2] < d.iloc[-2] and k.iloc[-1] > d.iloc[-1]
    cross_down = k.iloc[-2] > d.iloc[-2] and k.iloc[-1] < d.iloc[-1]

    if cross_up and st_dir.iloc[-1]:
        signal = 'buy'
    elif cross_down and not st_dir.iloc[-1]:
        signal = 'sell'

    if signal:
        signal_price = df.iloc[-2]['close']
        deviation = abs(price - signal_price) / signal_price
        if deviation > FRESH_SIGNAL_MAX_PRICE_DEVIATION:
            print("signal not fresh")
            return None
        return signal

    print("no signal, not opening trade")
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
