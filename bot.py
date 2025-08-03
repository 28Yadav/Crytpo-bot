# FILE: trading_bot.py

import time
import pandas as pd
import ccxt
import uuid
from decimal import Decimal, getcontext
from news import is_news_signal, combine_signals, check_opposite_news, start_news_loop

getcontext().prec = 18

# =============== CONFIG =================
SYMBOLS = ['ETH/USDT:USDT', 'BTC/USDT:USDT']
TIMEFRAME = '15m'
ORDER_SIZE_BY_SYMBOL = {
    'ETH/USDT:USDT': Decimal('0.09')
}
TP_MULTIPLIER = Decimal('2')
SL_MULTIPLIER = Decimal('7.0')
COOLDOWN_PERIOD = 60 * 30
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
last_position_side = {symbol: None for symbol in SYMBOLS}

# ============= HELPERS ===============
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

def in_position(symbol):
    positions = exchange.fetch_positions([symbol])
    for pos in positions:
        if float(pos.get('contracts', 0)) != 0:
            return True
    return False

# ============= INDICATORS =============
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

def compute_stochastic(df, k_period=14, d_period=3):
    low_min = df['low'].rolling(window=k_period).min()
    high_max = df['high'].rolling(window=k_period).max()
    k = 100 * (df['close'] - low_min) / (high_max - low_min)
    d = k.rolling(window=d_period).mean()
    return k, d

def compute_adx(df, period=14):
    plus_dm = df['high'].diff()
    minus_dm = df['low'].diff().abs()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    trur = compute_atr(df, period)
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / trur)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / trur)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    return dx.rolling(window=period).mean()

def is_fresh_signal(df):
    if len(df) < 50:
        print("📉 Not enough data")
        return None

    st_dir, atr = compute_supertrend(df)
    k, d = compute_stochastic(df)
    adx = compute_adx(df)

    cross_up = k.iloc[-2] < d.iloc[-2] and k.iloc[-1] > d.iloc[-1]
    cross_down = k.iloc[-2] > d.iloc[-2] and k.iloc[-1] < d.iloc[-1]

    if atr.iloc[-1] < VOLATILITY_THRESHOLD:
        print("🔇 Low volatility")
        return None

    signal = None
    price = df.iloc[-1]['close']
    signal_price = df.iloc[-2]['close']
    deviation = abs(price - signal_price) / signal_price

    if cross_up and st_dir.iloc[-1] and adx.iloc[-1] > 20 and deviation <= FRESH_SIGNAL_MAX_PRICE_DEVIATION:
        signal = 'buy'
    elif cross_down and not st_dir.iloc[-1] and adx.iloc[-1] > 20 and deviation <= FRESH_SIGNAL_MAX_PRICE_DEVIATION:
        signal = 'sell'

    return (signal, atr.iloc[-1]) if signal else None

# ============== ORDER ================
def place_order(symbol, side, entry_price, atr):
    print(f"🛒 Placing {side.upper()} order on {symbol}")
    try:
        entry_price = float(entry_price)
        atr = float(atr)
        qty = float(ORDER_SIZE_BY_SYMBOL.get(symbol, Decimal('0')))
    except Exception as e:
        print(f"[Qty/ATR Error] {e}")
        return

    try:
        exchange.set_position_mode(True)
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
        exchange.create_order(symbol, 'market', side, qty, None, order_params)
    except ccxt.InsufficientFunds as e:
        print(f"[FAILURE] Order rejected: {str(e)}")
        return

    tp_price = round(entry_price + atr * float(TP_MULTIPLIER) if side == 'buy' else entry_price - atr * float(TP_MULTIPLIER), 2)
    sl_price = round(entry_price - atr * float(SL_MULTIPLIER) if side == 'buy' else entry_price + atr * float(SL_MULTIPLIER), 2)

    try:
        exchange.create_order(symbol, 'take_profit_market', 'sell' if side == 'buy' else 'buy', qty, None, {
            'triggerPrice': tp_price,
            'positionSide': leverage_side,
            'newClientOrderId': generate_client_order_id(),
            'stopPrice': tp_price
        })
        exchange.create_order(symbol, 'stop_market', 'sell' if side == 'buy' else 'buy', qty, None, {
            'triggerPrice': sl_price,
            'positionSide': leverage_side,
            'newClientOrderId': generate_client_order_id(),
            'stopPrice': sl_price
        })
    except Exception as e:
        print(f"[TP/SL Error] {e}")

    last_trade_time[symbol] = time.time()
    last_position_side[symbol] = side

# ============== LOGIC =================
def trade_logic(symbol):
    print(f"🔍 Analyzing {symbol}")
    if in_position(symbol):
        check_opposite_news(last_position_side[symbol])
        print(f"⛔️ Already in position for {symbol}")
        return

    if time.time() - last_trade_time[symbol] < COOLDOWN_PERIOD:
        print("⏳ Cooldown...")
        return

    df = fetch_ohlcv(symbol, TIMEFRAME)
    tech_signal = is_fresh_signal(df)
    news_signal = is_news_signal()

    final_signal = combine_signals(tech_signal, news_signal)
    if not final_signal:
        print("🚫 No valid signal")
        return

    signal, atr = final_signal if tech_signal else (final_signal[0], compute_atr(df).iloc[-1])
    price = df.iloc[-1]['close']
    place_order(symbol, signal, price, atr)
    print(f"✅ {signal.upper()} {symbol}")

# ============== MAIN ==================
if __name__ == '__main__':
    from news import start_news_loop
    start_news_loop()
    print("🚀 Trading bot started...")
    while True:
        for symbol in SYMBOLS:
            try:
                trade_logic(symbol)
            except Exception as e:
                print(f"[Unhandled Error] {e}")
        print("⏰ Cycle complete, sleeping 60 seconds...")
        time.sleep(60)
