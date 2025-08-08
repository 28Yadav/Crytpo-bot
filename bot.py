import os
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
ORDER_SIZE_BY_SYMBOL = {
    'ETH/USDT:USDT': Decimal('0.09')  # Interpret per your exchange (contracts/base/quote)
}

# Volatility thresholds (ATR as percentage of price)
VOLATILITY_THRESHOLD_PCT = Decimal('0.08')  # minimum ATR% (e.g. 0.08 means 0.08%) to allow trading
VOL_LOW_PCT = Decimal('0.20')   # ATR% thresholds for TP/SL sizing (percent units, not fraction)
VOL_HIGH_PCT = Decimal('0.60')

# ATR-based multipliers for SL and TP depending on volatility bucket
SL_MULT_LOW = Decimal('1.0')
TP_MULT_LOW = Decimal('2.0')
SL_MULT_MID = Decimal('1.5')
TP_MULT_MID = Decimal('3.0')
SL_MULT_HIGH = Decimal('2.5')
TP_MULT_HIGH = Decimal('4.0')

COOLDOWN_PERIOD = 60 * 30
FRESH_SIGNAL_MAX_AGE_CANDLES = 1
FRESH_SIGNAL_MAX_PRICE_DEVIATION = 0.006

# Read API credentials from environment variables (safer)
API_KEY = os.getenv('BINGX_API_KEY', '')
API_SECRET = os.getenv('BINGX_API_SECRET', '')

exchange = ccxt.bingx({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
    }
})

last_trade_time = {symbol: 0 for symbol in SYMBOLS}

# ================== HELPERS ==================
def generate_client_order_id():
    return "ccbot-" + uuid.uuid4().hex[:16]

# ================== DATA FETCH ================
def fetch_ohlcv(symbol, timeframe, limit=150):
    print(f"📈 Fetching OHLCV for {symbol}...")
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    return df

# ================== ORDER EXECUTION =======================
def place_order(symbol, side, entry_price, atr):
    """Place market order and create ATR-based TP and SL orders.
    Returns True if main order placed (and attempts for TP/SL were made).
    """
    print(f"🛒 Placing {side.upper()} order on {symbol}...")
    try:
        entry_price = float(entry_price)
        atr = float(atr)
        qty = float(ORDER_SIZE_BY_SYMBOL.get(symbol, Decimal('0')))
    except Exception as e:
        print(f"[Qty/ATR Error] {e}")
        return False

    print(f"[DEBUG] Qty: {qty}")

    # attempt to set leverage / position mode (best-effort)
    try:
        if hasattr(exchange, 'set_position_mode'):
            try:
                exchange.set_position_mode(True)
            except Exception as e:
                print(f"[Mode Warning] Could not set position mode: {e}")
    except Exception:
        pass

    try:
        leverage_side = 'LONG' if side == 'buy' else 'SHORT'
        try:
            exchange.set_leverage(15, symbol, params={'marginMode': 'cross', 'side': leverage_side})
        except Exception as e:
            print(f"[Leverage Warning] set_leverage failed or unsupported: {e}")
    except Exception as e:
        print(f"[Leverage Error] {e}")

    order_params = {
        'positionSide': leverage_side,
        'newClientOrderId': generate_client_order_id()
    }

    try:
        order = exchange.create_order(symbol, 'market', side, qty, None, order_params)
        print(f"[Order] Market order placed: {order}")
    except ccxt.InsufficientFunds as e:
        print(f"[FAILURE] Order rejected: {str(e)}")
        return False
    except Exception as e:
        print(f"[Order Error] {e}")
        return False

    # Calculate dynamic TP/SL based on ATR & current price
    tp_price, sl_price = calculate_tp_sl(entry_price, atr, side)

    # Try to place TP and SL - best-effort; exchange param names may vary
    try:
        tp_order = exchange.create_order(symbol, 'take_profit_market', 'sell' if side == 'buy' else 'buy', qty, None, {
            'triggerPrice': tp_price,
            'positionSide': leverage_side,
            'newClientOrderId': generate_client_order_id(),
            'stopPrice': tp_price
        })
        print(f"[TP Order] Created at {tp_price}")
    except Exception as e:
        print(f"[TP Error] {e}")

    try:
        sl_order = exchange.create_order(symbol, 'stop_market', 'sell' if side == 'buy' else 'buy', qty, None, {
            'triggerPrice': sl_price,
            'positionSide': leverage_side,
            'newClientOrderId': generate_client_order_id(),
            'stopPrice': sl_price
        })
        print(f"[SL Order] Created at {sl_price}")
    except Exception as e:
        print(f"[SL Error] {e}")

    last_trade_time[symbol] = time.time()
    return True

# ================== INDICATORS ==================
def compute_atr(df, period=14):
    high = df['high']
    low = df['low']
    close = df['close']

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.rolling(window=period, min_periods=1).mean()
    return atr


def compute_supertrend(df, period=10, multiplier=3):
    atr = compute_atr(df, period)
    hl2 = (df['high'] + df['low']) / 2
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    final_upper = upperband.copy()
    final_lower = lowerband.copy()
    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=bool)

    for i in range(len(df)):
        if i == 0:
            final_upper.iat[i] = upperband.iat[i]
            final_lower.iat[i] = lowerband.iat[i]
            supertrend.iat[i] = final_upper.iat[i]
            direction.iat[i] = False
            continue

        final_upper.iat[i] = upperband.iat[i] if (upperband.iat[i] < final_upper.iat[i-1] or df['close'].iat[i-1] > final_upper.iat[i-1]) else final_upper.iat[i-1]
        final_lower.iat[i] = lowerband.iat[i] if (lowerband.iat[i] > final_lower.iat[i-1] or df['close'].iat[i-1] < final_lower.iat[i-1]) else final_lower.iat[i-1]

        if supertrend.iat[i-1] == final_upper.iat[i-1] and df['close'].iat[i] <= final_upper.iat[i]:
            supertrend.iat[i] = final_upper.iat[i]
            direction.iat[i] = False
        elif supertrend.iat[i-1] == final_upper.iat[i-1] and df['close'].iat[i] > final_upper.iat[i]:
            supertrend.iat[i] = final_lower.iat[i]
            direction.iat[i] = True
        elif supertrend.iat[i-1] == final_lower.iat[i-1] and df['close'].iat[i] >= final_lower.iat[i]:
            supertrend.iat[i] = final_lower.iat[i]
            direction.iat[i] = True
        elif supertrend.iat[i-1] == final_lower.iat[i-1] and df['close'].iat[i] < final_lower.iat[i]:
            supertrend.iat[i] = final_upper.iat[i]
            direction.iat[i] = False
        else:
            direction.iat[i] = True if df['close'].iat[i] > final_lower.iat[i] else False
            supertrend.iat[i] = final_lower.iat[i] if direction.iat[i] else final_upper.iat[i]

    return direction, atr


def compute_stochastic(df, k_period=14, d_period=3):
    low_min = df['low'].rolling(window=k_period, min_periods=1).min()
    high_max = df['high'].rolling(window=k_period, min_periods=1).max()
    denom = (high_max - low_min).replace(0, np.nan)
    k = 100 * (df['close'] - low_min) / denom
    k = k.fillna(50)
    d = k.rolling(window=d_period, min_periods=1).mean()
    return k, d

# ================== TP/SL CALC ==================
def calculate_tp_sl(entry_price, atr, side):
    """Calculate dynamic TP and SL prices using ATR and volatility buckets.
    entry_price: float
    atr: float (price units)
    side: 'buy' or 'sell'
    returns (tp_price, sl_price)
    """
    price = Decimal(str(entry_price))
    atr_dec = Decimal(str(atr))
    atr_pct = (atr_dec / price) * Decimal('100') if price != 0 else Decimal('0')

    # Determine volatility bucket
    if atr_pct <= VOL_LOW_PCT:
        sl_mult = SL_MULT_LOW
        tp_mult = TP_MULT_LOW
    elif atr_pct <= VOL_HIGH_PCT:
        sl_mult = SL_MULT_MID
        tp_mult = TP_MULT_MID
    else:
        sl_mult = SL_MULT_HIGH
        tp_mult = TP_MULT_HIGH

    sl_distance = (atr_dec * sl_mult)
    tp_distance = (atr_dec * tp_mult)

    if side == 'buy':
        sl_price = float((price - sl_distance).quantize(Decimal('0.01')))
        tp_price = float((price + tp_distance).quantize(Decimal('0.01')))
    else:
        sl_price = float((price + sl_distance).quantize(Decimal('0.01')))
        tp_price = float((price - tp_distance).quantize(Decimal('0.01')))

    print(f"[TP/SL] ATR%={atr_pct:.4f}%, SL_mult={sl_mult}, TP_mult={tp_mult}, TP={tp_price}, SL={sl_price}")
    return tp_price, sl_price

# ================== SIGNALS ==================
def is_fresh_signal(df):
    if len(df) < 50:
        print("📉 Not enough data to generate signals.")
        return None

    st_dir, atr = compute_supertrend(df)
    k, d = compute_stochastic(df)

    cross_up = (k.iloc[-2] < d.iloc[-2]) and (k.iloc[-1] > d.iloc[-1])
    cross_down = (k.iloc[-2] > d.iloc[-2]) and (k.iloc[-1] < d.iloc[-1])

    price = df['close'].iloc[-1]
    signal_price = df['close'].iloc[-2]
    deviation = abs(price - signal_price) / signal_price if signal_price != 0 else 1

    atr_latest = atr.iloc[-1]
    atr_pct = Decimal(str(atr_latest / price * 100)) if price != 0 else Decimal('0')

    print(f"[DEBUG] ATR (price): {atr_latest:.6f}, ATR%: {atr_pct:.6f}")
    if atr_pct < VOLATILITY_THRESHOLD_PCT:
        print("🔇 Skipping due to low volatility (ATR% below threshold)")
        return None

    print(f"[DEBUG] Stochastic: K={k.iloc[-1]:.2f}, D={d.iloc[-1]:.2f}, cross_up={cross_up}, cross_down={cross_down}")
    print(f"[DEBUG] Price deviation: {deviation:.6f}")

    signal = None
    try:
        st_is_up = bool(st_dir.iloc[-1])
    except Exception:
        st_is_up = False

    if cross_up and st_is_up and deviation <= FRESH_SIGNAL_MAX_PRICE_DEVIATION:
        signal = 'buy'
    elif cross_down and (not st_is_up) and deviation <= FRESH_SIGNAL_MAX_PRICE_DEVIATION:
        signal = 'sell'

    if not signal:
        print("🚫 Conditions not met for signal.")
        return None

    # Freshness check: ensure the cross happened on the previous candle (age = 1)
    age_candles = 1
    if age_candles > FRESH_SIGNAL_MAX_AGE_CANDLES:
        print("⌛ Signal too old")
        return None

    return (signal, atr_latest)

# ================== LOGIC ======================

def in_position(symbol):
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            contracts = pos.get('contracts') or pos.get('size') or pos.get('positionAmt') or 0
            if float(contracts) != 0:
                return True
    except Exception as e:
        print(f"[in_position warning] {e}")
    return False


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
    price = df['close'].iloc[-1]
    success = place_order(symbol, signal, price, atr)
    if success:
        print(f"✅ {signal.upper()} {symbol}")
        return True

    print("❌ Order placement failed")
    return False

# ================== MAIN =====================
if __name__ == '__main__':
    print("🚀 Trading bot started with ATR-based TP/SL...")
    while True:
        for symbol in SYMBOLS:
            try:
                trade_logic(symbol)
            except Exception as e:
                print(f"[Unhandled Error] {e}")
        print("⏰ Cycle complete, sleeping 60 seconds...")
        time.sleep(30)
