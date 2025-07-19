# volume_filtered_trade_bot.py

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
    'ETH/USDT:USDT': Decimal('0.02'),
    'BTC/USDT:USDT': Decimal('0.00068')
}
TP_PERCENT = Decimal('0.01')
SL_PERCENT = Decimal('0.06') 
COOLDOWN_PERIOD = 60 * 60 * 2  # 4 hours


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
    print(f"\U0001F4C8 Fetching OHLCV for {symbol}...")
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


# ================== ORDER EXECUTION ===================
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
def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def compute_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    vwap = (tp * df['volume']).cumsum() / df['volume'].cumsum()
    return vwap

def is_fresh_signal(df):
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    last = df.iloc[-1]
    return (
        (prev['ema_9'] <= prev['ema_21'] and last['ema_9'] > last['ema_21']) or
        (prev['ema_9'] >= prev['ema_21'] and last['ema_9'] < last['ema_21'])
    )

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
    df['vwap'] = compute_vwap(df)
    df['ema_9'] = compute_ema(df['close'], 9)
    df['ema_21'] = compute_ema(df['close'], 21)

    last = df.iloc[-1]
    price = last['close']
    vwap = last['vwap']
    ema9 = last['ema_9']
    ema21 = last['ema_21']

    if not is_fresh_signal(df):
        print("⚠️ Signal not fresh. Skipping...")
        return False

    if ema9 > ema21 and price > vwap:
        place_order(symbol, 'buy', price)
        print(f"✅ LONG {symbol}")
        return True

    elif ema9 < ema21 and price < vwap:
        place_order(symbol, 'sell', price)
        print(f"✅ SHORT {symbol}")
        return True

    else:
        print("⏸️ No trade condition met")
        return False

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
