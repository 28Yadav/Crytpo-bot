import time
import pandas as pd
import numpy as np
import datetime
import ccxt
import uuid
from decimal import Decimal, getcontext

getcontext().prec = 18

# ================== CONFIG ===================

SYMBOLS = ['ETH/USDT:USDT']
TIMEFRAME = '15m'
ORDER_SIZE_ETH = Decimal('0.02')
TP_PERCENT = Decimal('0.01')
SL_PERCENT = Decimal('0.02')

exchange = ccxt.bingx({
    'apiKey': "TqS2UwImeJdxlVJw2t255c4rpcjcey2RxyTFUeI1xklzvt76gIq6YGV6UxsuElxE08C39i293hSEEUgr4Mgqg",
    'secret': "hJmuhVSclYzL8UGcuBzw3NrVjF18WZlYt1Zm6SdZa1n0a3nq2POCYoDhKGnIGmmF5Kt8O1XIk6fIpOigJd8Q",
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
    }
})

last_trade_time = {}
cooldown_period = 3600  # seconds

# ================== DATA FETCH ================
def fetch_ohlcv(symbol, timeframe, limit=150):
    print(f"📈 Fetching OHLCV for {symbol}...")
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

# ================== BALANCE ===================
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
        qty = float(ORDER_SIZE_ETH)
    except Exception as e:
        print(f"[Qty Error] {e}")
        return

    print(f"[DEBUG] Qty: {qty}")

    try:
        exchange.set_position_mode(True)
        print(f"[DEBUG] Position Mode: Hedge")
    except Exception as e:
        print(f"[Mode Error] {e}")
        return

    try:
        leverage_side = 'LONG' if side == 'buy' else 'SHORT'
        exchange.set_leverage(15, symbol, params={'side': leverage_side})
        print(f"[DEBUG] Leverage set to 15x {leverage_side} for {symbol}")
    except Exception as e:
        print(f"[Leverage Error] {e}")
        return

    order_params = {
        'marginMode': 'isolated',
        'positionSide': leverage_side,
        'type': 'swap',
        'clientOrderId': generate_client_order_id()
    }
    print(f"[DEBUG] Order Params: {order_params}")

    try:
        order = exchange.create_order(symbol, 'market', side, qty, None, order_params)
        print(f"[ORDER SUCCESS] Order placed with qty {qty}")
    except ccxt.InsufficientFunds as e:
        print(f"[FAILURE] Order rejected due to insufficient funds: {str(e)}")
        return

    return order

def close_position(symbol, side):
    try:
        qty = float(ORDER_SIZE_ETH)
        opposite_side = 'sell' if side == 'buy' else 'buy'
        position_side = 'LONG' if side == 'buy' else 'SHORT'
        exchange.create_order(symbol, 'market', opposite_side, qty, None, {
            'positionSide': position_side,
            'marginMode': 'isolated'
        })
        print(f"🔁 Closed {position_side} due to signal flip")
    except Exception as e:
        print(f"[Close Position Error] {e}")

def in_position(symbol):
    positions = exchange.fetch_positions([symbol])
    for pos in positions:
        if float(pos.get('contracts', 0)) != 0:
            return pos
    return None

# ================== STRATEGY ==================
def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    vwap = (tp * df['volume']).cumsum() / df['volume'].cumsum()
    return vwap

def trade_logic(symbol):
    global last_trade_time
    print(f"🔍 Analyzing {symbol}...")

    now = time.time()
    if symbol in last_trade_time and now - last_trade_time[symbol] < cooldown_period:
        print(f"⏳ Cooldown active for {symbol}")
        return False

    df = fetch_ohlcv(symbol, TIMEFRAME)
    df['vwap'] = compute_vwap(df)
    df['ema_9'] = compute_ema(df['close'], 9)
    df['ema_21'] = compute_ema(df['close'], 21)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = last['close']
    vwap = last['vwap']
    ema9 = last['ema_9']
    ema21 = last['ema_21']

    avg_vol = df['volume'].rolling(20).mean().iloc[-1]
    if last['volume'] < avg_vol:
        print("⚠️ Volume too low — skipping entry.")
        return False

    if abs(ema9 - ema21) / price < 0.002:
        print("⛔ EMA too close — flat market, skipping.")
        return False

    print(f"📊 Price: {price}, VWAP: {vwap:.2f}, EMA9: {ema9:.2f}, EMA21: {ema21:.2f}")
    print(get_balance())

    current_position = in_position(symbol)
    signal = None

    if (prev['ema_9'] <= prev['ema_21']) and (ema9 > ema21) and price > vwap:
        signal = 'buy'
    elif (prev['ema_9'] >= prev['ema_21']) and (ema9 < ema21) and price < vwap:
        signal = 'sell'

    if current_position:
        pos_side = current_position['side'].lower()
        if signal and signal != pos_side:
            close_position(symbol, pos_side)
            last_trade_time[symbol] = now
            return True
        else:
            print(f"🧭 Holding current {pos_side} position")
            return False

    if signal:
        place_order(symbol, signal, price)
        last_trade_time[symbol] = now
        print(f"✅ {signal.upper()} {symbol}")
        return True

    print(f"⏸️ No trade condition met for {symbol}")
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
        time.sleep(60)
