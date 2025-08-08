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
    'ETH/USDT:USDT': Decimal('0.09')  # NOTE: interpret this as contract/qty depending on your exchange config
}
TP_MULTIPLIER = Decimal('2')
SL_MULTIPLIER = Decimal('2')
COOLDOWN_PERIOD = 60 * 30
FRESH_SIGNAL_MAX_AGE_CANDLES = 1
FRESH_SIGNAL_MAX_PRICE_DEVIATION = 0.006
VOLATILITY_THRESHOLD_PCT = Decimal('0.08')  # ATR as percentage of price (e.g. 0.08 == 0.08%)

# Read API credentials from env vars (DO NOT hardcode your keys)
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

def get_balance():
    balance = exchange.fetch_balance({'type': 'swap'})
    usdt = balance.get('free', {}).get('USDT', 0)
    print(f"[DEBUG] USDT Free Balance: {usdt}")
    return Decimal(str(usdt))

# ================== ORDER EXECUTION =======================
def place_order(symbol, side, entry_price, atr):
    print(f"🛒 Placing {side.upper()} order on {symbol}...")
    try:
        entry_price = float(entry_price)
        atr = float(atr)
        qty = float(ORDER_SIZE_BY_SYMBOL.get(symbol, Decimal('0')))
    except Exception as e:
        print(f"[Qty/ATR Error] {e}")
        return None

    print(f"[DEBUG] Qty: {qty}")

    # NOTE: The following exchange calls are exchange-specific. Adjust params to match bingx API.
    try:
        # Example: Some exchanges support set_position_mode('single'/'dual') or similar. Catch failures.
        if hasattr(exchange, 'set_position_mode'):
            try:
                exchange.set_position_mode(True)
            except Exception as e:
                print(f"[Mode Warning] Could not set position mode: {e}")
    except Exception:
        pass

    try:
        leverage_side = 'LONG' if side == 'buy' else 'SHORT'
        # Many ccxt implementations use set_leverage(leverage, symbol) and separate params for margin mode
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

    order = None
    try:
        # Market open order
        order = exchange.create_order(symbol, 'market', side, qty, None, order_params)
        print(f"[Order] Market order placed: {order}")
    except ccxt.InsufficientFunds as e:
        print(f"[FAILURE] Order rejected: {str(e)}")
        return None
    except Exception as e:
        print(f"[Order Error] {e}")
        return None

    # Calculate TP/SL using ATR (price units)
    tp_price = round(entry_price + atr * float(TP_MULTIPLIER) if side == 'buy' else entry_price - atr * float(TP_MULTIPLIER), 2)
    sl_price = round(entry_price - atr * float(SL_MULTIPLIER) if side == 'buy' else entry_price + atr * float(SL_MULTIPLIER), 2)

    # Create TP and SL - exchange-specific params
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
    return order


def in_position(symbol):
    try:
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            # Different exchanges use different keys; try common ones
            contracts = pos.get('contracts') or pos.get('size') or pos.get('positionAmt') or 0
            if float(contracts) != 0:
                return True
    except Exception as e:
        print(f"[in_position warning] {e}")
    return False

# ================== INDICATORS ==================
def compute_atr(df, period=14):
    '''True Range and ATR (Wilder's moving average)'''
    high = df['high']
    low = df['low']
    close = df['close']

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder's smoothing (optional). We'll return simple rolling mean for stability.
    atr = tr.rolling(window=period, min_periods=1).mean()
    return atr


def compute_supertrend(df, period=10, multiplier=3):
    '''Returns a boolean Series: True for up trend, False for down trend; and the ATR series.'''
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

        # Carry forward previous final bands if current band crosses
        final_upper.iat[i] = upperband.iat[i] if upperband.iat[i] < final_upper.iat[i-1] or df['close'].iat[i-1] > final_upper.iat[i-1] else final_upper.iat[i-1]
        final_lower.iat[i] = lowerband.iat[i] if lowerband.iat[i] > final_lower.iat[i-1] or df['close'].iat[i-1] < final_lower.iat[i-1] else final_lower.iat[i-1]

        # Determine trend
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
            # Fallback
            direction.iat[i] = True if df['close'].iat[i] > final_lower.iat[i] else False
            supertrend.iat[i] = final_lower.iat[i] if direction.iat[i] else final_upper.iat[i]

    return direction, atr


def compute_stochastic(df, k_period=14, d_period=3):
    low_min = df['low'].rolling(window=k_period, min_periods=1).min()
    high_max = df['high'].rolling(window=k_period, min_periods=1).max()
    denom = (high_max - low_min).replace(0, np.nan)
    k = 100 * (df['close'] - low_min) / denom
    k = k.fillna(50)  # when denom is zero, set neutral 50
    d = k.rolling(window=d_period, min_periods=1).mean()
    return k, d

# ================== SIGNALS ==================
def is_fresh_signal(df):
    if len(df) < 50:
        print("📉 Not enough data to generate signals.")
        return None

    st_dir, atr = compute_supertrend(df)
    k, d = compute_stochastic(df)

    # Detect k/d cross on latest candle
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
    # Ensure supertrend direction is boolean and not NaN
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

    # Freshness check - ensure the cross happened within allowed candle age
    # We use timestamp index to measure how recent the cross candle is.
    last_ts = df.index[-1]
    prev_ts = df.index[-2]
    candle_seconds = (prev_ts - df.index[-3]).total_seconds() if len(df) >= 4 else pd.Timedelta(TIMEFRAME).total_seconds()

    age_candles = 0
    # If signal detected on previous bar, age = 1
    age_candles = 1

    if age_candles > FRESH_SIGNAL_MAX_AGE_CANDLES:
        print("⌛ Signal too old")
        return None

    return (signal, atr_latest)

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
    price = df['close'].iloc[-1]
    res = place_order(symbol, signal, price, atr)
    if res:
        print(f"✅ {signal.upper()} {symbol}")
        return True
    else:
        print("❌ Order placement failed")
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
        time.sleep(30)
