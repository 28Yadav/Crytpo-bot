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
    'ETH/USDT:USDT': Decimal('0.07')
}
TP_MULTIPLIER = Decimal('2')
SL_MULTIPLIER = Decimal('2.0')
COOLDOWN_PERIOD = 60 * 30
FRESH_SIGNAL_MAX_AGE_CANDLES = 1
FRESH_SIGNAL_MAX_PRICE_DEVIATION = 0.006
VOLATILITY_THRESHOLD = 0.5

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

# ================== ORDER EXECUTION =======================
def place_order(symbol, side, entry_price, atr):
    print(f"🛒 Placing {side.upper()} order on {symbol}...")
    try:
        entry_price = float(entry_price)
        atr = float(atr)
        qty = float(ORDER_SIZE_BY_SYMBOL.get(symbol, Decimal('0')))
    except Exception as e:
        print(f"[Qty/ATR Error] {e}")
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
        'positionSide': leverage_side,
        'newClientOrderId': generate_client_order_id()
    }

    try:
        order = exchange.create_order(symbol, 'market', side, qty, None, order_params)
    except ccxt.InsufficientFunds as e:
        print(f"[FAILURE] Order rejected: {str(e)}")
        return

    tp_price = round(entry_price + atr * float(TP_MULTIPLIER) if side == 'buy' else entry_price - atr * float(TP_MULTIPLIER), 2)
    sl_price = round(entry_price - atr * float(SL_MULTIPLIER) if side == 'buy' else entry_price + atr * float(SL_MULTIPLIER), 2)

    try:
        tp_order = exchange.create_order(symbol, 'take_profit_market', 'sell' if side == 'buy' else 'buy', qty, None, {
            'triggerPrice': tp_price,
            'positionSide': leverage_side,
            'newClientOrderId': generate_client_order_id(),
            'stopPrice': tp_price
        })
        print(f"[TP Order] Created at {tp_price}")

        sl_order = exchange.create_order(symbol, 'stop_market', 'sell' if side == 'buy' else 'buy', qty, None, {
            'triggerPrice': sl_price,
            'positionSide': leverage_side,
            'newClientOrderId': generate_client_order_id(),
            'stopPrice': sl_price
        })
        print(f"[SL Order] Created at {sl_price}")

    except Exception as e:
        print(f"[TP/SL Error] {e}")

    last_trade_time[symbol] = time.time()
    return order


def in_position(symbol):
    positions = exchange.fetch_positions([symbol])
    for pos in positions:
        if float(pos.get('contracts', 0)) != 0:
            return True
    return False

# ================== INDICATORS ==================
def compute_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift())
    low_close = abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def compute_supertrend(df, period=10, multiplier=3):
    atr = compute_atr(df, period)
    hl2 = (df['high'] + df['low']) / 2
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    supertrend = pd.Series(index=df.index)
    direction = pd.Series(index=df.index, dtype=bool)

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

    return direction, atr

def compute_ob_indicator(df, length=14, overbought=70, oversold=30):
    # Using RSI as OB/OS indicator
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.rolling(length).mean()
    avg_loss = loss.rolling(length).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi, overbought, oversold

def compute_adx(df, period=14):
    plus_dm = df['high'].diff()
    minus_dm = df['low'].diff().abs()

    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0

    trur = compute_atr(df, period)
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / trur)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / trur)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    adx = dx.rolling(window=period).mean()
    return adx

def is_fresh_signal(df):
    if len(df) < 50:
        print("📉 Not enough data to generate signals.")
        return None

    st_dir, atr = compute_supertrend(df)
    rsi, ob_level, os_level = compute_ob_indicator(df)
    adx = compute_adx(df)

    overbought_cross = rsi.iloc[-2] < ob_level and rsi.iloc[-1] >= ob_level
    oversold_cross = rsi.iloc[-2] > os_level and rsi.iloc[-1] <= os_level

    print(f"[DEBUG] ATR: {atr.iloc[-1]:.4f}")
    if atr.iloc[-1] < VOLATILITY_THRESHOLD:
        print("🔇 Skipping due to low volatility")
        return None

    signal = None
    price = df.iloc[-1]['close']
    signal_price = df.iloc[-2]['close']
    deviation = abs(price - signal_price) / signal_price

    print(f"[DEBUG] RSI: {rsi.iloc[-1]:.2f}, overbought_cross={overbought_cross}, oversold_cross={oversold_cross}")
    print(f"[DEBUG] ADX: {adx.iloc[-1]:.2f}")
    print(f"[DEBUG] Price deviation: {deviation:.4f}")

    if oversold_cross and st_dir.iloc[-1] and adx.iloc[-1] > 20 and deviation <= FRESH_SIGNAL_MAX_PRICE_DEVIATION:
        signal = 'buy'
    elif overbought_cross and not st_dir.iloc[-1] and adx.iloc[-1] > 20 and deviation <= FRESH_SIGNAL_MAX_PRICE_DEVIATION:
        signal = 'sell'

    if not signal:
        print("🚫 Conditions not met for signal.")
        return None

    return (signal, atr.iloc[-1])

# ================== LOGIC ======================
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
    signal_result = is_fresh_signal(df)
    if not signal_result:
        return False

    signal, atr = signal_result
    price = df.iloc[-1]['close']
    place_order(symbol, signal, price, atr)
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
        time.sleep(30)



