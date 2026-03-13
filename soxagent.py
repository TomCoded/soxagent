#!/usr/bin/env python3
"""
SOXAgent - Automated SOXL trading agent using the Schwab Trader API.

Checks SOXL price every 15 minutes.
- Buys $200 if down 10%+ from open, no buys today, $250+ cash, under $400/wk.
- Sells $200 if up 10%+ from open, no sells today, under 2 sells/wk.
"""

import argparse
import fcntl
import json
import math
import os
import sys
import time
from datetime import datetime, date, timedelta

import schwab
from schwab.orders.equities import equity_buy_limit, equity_sell_limit

SYMBOL = "SOXL"
DIP_THRESHOLD = -0.10  # -10%
SURGE_THRESHOLD = 0.10  # +10%
MIN_CASH = 250.0
BUY_AMOUNT = 200.0
SELL_AMOUNT = 200.0
WEEKLY_BUY_LIMIT = 400.0
WEEKLY_SELL_LIMIT = 2  # max sell orders per week
LIMIT_ORDER_DRIFT = 0.01  # 1% buffer on limit prices to help fills
CHECK_INTERVAL_SECONDS = 15 * 60  # 15 minutes
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".soxagent.lock")


def acquire_lock():
    """Acquire a file lock to prevent multiple instances from running."""
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[error] Another instance of soxagent is already running.")
        sys.exit(1)
    return lock_fd


def load_env():
    """Load .env file if present (simple key=value parsing)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def get_client():
    """Create an authenticated Schwab API client."""
    app_key = os.environ["SCHWAB_APP_KEY"]
    app_secret = os.environ["SCHWAB_APP_SECRET"]
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
    token_path = os.environ.get("SCHWAB_TOKEN_PATH", "./token.json")

    try:
        # Try to use existing token
        client = schwab.auth.client_from_token_file(token_path, app_key, app_secret)
        print("[auth] Loaded existing token.")
    except FileNotFoundError:
        # First-time auth: opens browser for OAuth login
        print("[auth] No token found. Starting OAuth flow...")
        print("       A browser will open. Log in with your Schwab brokerage credentials.")
        print("       After authorizing, copy the full redirect URL and paste it here.")
        client = schwab.auth.client_from_manual_flow(
            app_key, app_secret, callback_url, token_path
        )
        print("[auth] Token saved.")

    return client


def get_account_hash(client):
    """Get the account hash, either from env or by listing accounts."""
    account_hash = os.environ.get("SCHWAB_ACCOUNT_HASH", "").strip()
    if account_hash:
        return account_hash

    resp = client.get_account_numbers()
    resp.raise_for_status()
    accounts = resp.json()
    if not accounts:
        raise RuntimeError("No accounts found.")
    # Use the first account
    account_hash = accounts[0]["hashValue"]
    print(f"[account] Using account ending in ...{accounts[0]['accountNumber'][-4:]}")
    return account_hash


def show_accounts(client):
    """Print all linked accounts and their hashes."""
    resp = client.get_account_numbers()
    resp.raise_for_status()
    for acct in resp.json():
        print(f"  Account: ...{acct['accountNumber'][-4:]}  Hash: {acct['hashValue']}")


def get_account_data(client, account_hash):
    """Return the full account data dict."""
    resp = client.get_account(account_hash, fields=[schwab.client.Client.Account.Fields.POSITIONS])
    resp.raise_for_status()
    return resp.json()


def get_cash_balance(account_data):
    """Return available cash from account data."""
    balances = account_data["securitiesAccount"]["currentBalances"]
    cash = balances.get("cashBalance", balances.get("availableFunds", 0.0))
    return float(cash)


def get_shares_held(account_data, symbol):
    """Return number of shares held for a symbol."""
    positions = account_data["securitiesAccount"].get("positions", [])
    for pos in positions:
        if pos.get("instrument", {}).get("symbol") == symbol:
            return float(pos.get("longQuantity", 0))
    return 0.0


def get_quote(client, symbol):
    """Return (last_price, open_price, pct_change) for a symbol."""
    resp = client.get_quote(symbol)
    resp.raise_for_status()
    data = resp.json()
    quote = data[symbol]["quote"]
    last = float(quote["lastPrice"])
    open_price = float(quote["openPrice"])
    pct_change = (last - open_price) / open_price if open_price else 0.0
    return last, open_price, pct_change


def has_orders_today(client, account_hash, symbol, instruction):
    """Check if any orders with the given instruction (BUY/SELL) were placed today for symbol."""
    today = datetime.now()
    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    resp = client.get_orders_for_account(
        account_hash,
        from_entered_datetime=start,
        to_entered_datetime=today,
    )
    resp.raise_for_status()
    for order in resp.json():
        for leg in order.get("orderLegCollection", []):
            if (leg.get("instrument", {}).get("symbol") == symbol
                    and leg.get("instruction") == instruction):
                return True
    return False


def get_weekly_spend(client, account_hash, symbol):
    """Return total dollar amount spent on buy orders for symbol in the past 7 days."""
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    resp = client.get_orders_for_account(
        account_hash,
        from_entered_datetime=week_ago,
        to_entered_datetime=now,
    )
    resp.raise_for_status()
    total = 0.0
    for order in resp.json():
        if order.get("orderLegCollection"):
            for leg in order["orderLegCollection"]:
                instrument = leg.get("instrument", {})
                if (instrument.get("symbol") == symbol
                        and leg.get("instruction") == "BUY"):
                    # Use filled quantity * average price if available
                    filled_qty = float(order.get("filledQuantity", 0))
                    price = float(order.get("price", 0))
                    if filled_qty and price:
                        total += filled_qty * price
                    else:
                        # Fall back to requested quantity * last price from order
                        qty = float(order.get("quantity", 0))
                        total += qty * price
    return total


def get_weekly_sell_count(client, account_hash, symbol):
    """Return number of sell orders for symbol in the past 7 days."""
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    resp = client.get_orders_for_account(
        account_hash,
        from_entered_datetime=week_ago,
        to_entered_datetime=now,
    )
    resp.raise_for_status()
    count = 0
    for order in resp.json():
        for leg in order.get("orderLegCollection", []):
            if (leg.get("instrument", {}).get("symbol") == symbol
                    and leg.get("instruction") == "SELL"):
                count += 1
    return count


def place_sell_order(client, account_hash, symbol, shares, current_price, dry_run=False):
    """Place a limit sell order with drift buffer below current price."""
    limit_price = round(current_price * (1 - LIMIT_ORDER_DRIFT), 2)
    if dry_run:
        print(f"[dry-run] Would SELL {shares} shares of {symbol} @ limit ${limit_price:.2f} (~${shares * limit_price:.2f})")
        return True
    order = equity_sell_limit(symbol, shares, limit_price)
    print(f"[order] Placing limit SELL for {shares} shares of {symbol} @ limit ${limit_price:.2f} (~${shares * limit_price:.2f})...")
    resp = client.place_order(account_hash, order)
    resp.raise_for_status()
    print(f"[order] Order placed successfully. Status: {resp.status_code}")
    return True


def place_buy_order(client, account_hash, symbol, shares, current_price, dry_run=False):
    """Place a limit buy order with drift buffer above current price."""
    limit_price = round(current_price * (1 + LIMIT_ORDER_DRIFT), 2)
    if dry_run:
        print(f"[dry-run] Would BUY {shares} shares of {symbol} @ limit ${limit_price:.2f} (~${shares * limit_price:.2f})")
        return True
    order = equity_buy_limit(symbol, shares, limit_price)
    print(f"[order] Placing limit BUY for {shares} shares of {symbol} @ limit ${limit_price:.2f} (~${shares * limit_price:.2f})...")
    resp = client.place_order(account_hash, order)
    resp.raise_for_status()
    print(f"[order] Order placed successfully. Status: {resp.status_code}")
    return True


def check_and_trade(client, account_hash, dry_run=False):
    """Run one check cycle: get quote, evaluate conditions, maybe buy or sell."""
    now = datetime.now()
    prefix = "[dry-run] " if dry_run else ""
    print(f"\n{prefix}[{now.strftime('%H:%M:%S')}] Checking {SYMBOL}...")

    # Get current quote
    last, open_price, pct_change = get_quote(client, SYMBOL)
    print(f"  Open: ${open_price:.2f}  Last: ${last:.2f}  Change: {pct_change:+.2%}")

    if pct_change <= DIP_THRESHOLD:
        check_buy(client, account_hash, last, pct_change, dry_run)
    elif pct_change >= SURGE_THRESHOLD:
        check_sell(client, account_hash, last, pct_change, dry_run)
    else:
        print(f"  No action (buy threshold: {DIP_THRESHOLD:.0%}, sell threshold: +{SURGE_THRESHOLD:.0%}).")


def check_buy(client, account_hash, last, pct_change, dry_run=False):
    """Evaluate buy conditions and place order if met."""
    print(f"  Down {pct_change:+.2%} — meets {DIP_THRESHOLD:.0%} buy threshold!")

    shares = math.floor(BUY_AMOUNT / last)
    if shares < 1:
        print(f"  Price ${last:.2f} too high to buy even 1 share with ${BUY_AMOUNT:.2f}. Skipping.")
        return

    if has_orders_today(client, account_hash, SYMBOL, "BUY"):
        print("  Already have a buy order today. Skipping.")
        return

    weekly_spend = get_weekly_spend(client, account_hash, SYMBOL)
    remaining = WEEKLY_BUY_LIMIT - weekly_spend
    print(f"  Weekly buy spend: ${weekly_spend:.2f} / ${WEEKLY_BUY_LIMIT:.2f} (${remaining:.2f} remaining)")

    if remaining < BUY_AMOUNT:
        print(f"  Weekly buy limit would be exceeded. Skipping.")
        return

    account_data = get_account_data(client, account_hash)
    cash = get_cash_balance(account_data)
    print(f"  Cash available: ${cash:.2f}")

    if cash < MIN_CASH:
        print(f"  Insufficient cash (need ${MIN_CASH:.2f}). Skipping.")
        return

    place_buy_order(client, account_hash, SYMBOL, shares, last, dry_run)


def check_sell(client, account_hash, last, pct_change, dry_run=False):
    """Evaluate sell conditions and place order if met."""
    print(f"  Up {pct_change:+.2%} — meets +{SURGE_THRESHOLD:.0%} sell threshold!")

    shares_wanted = math.floor(SELL_AMOUNT / last)
    if shares_wanted < 1:
        print(f"  Price ${last:.2f} too high to sell even 1 share for ${SELL_AMOUNT:.2f}. Skipping.")
        return

    if has_orders_today(client, account_hash, SYMBOL, "SELL"):
        print("  Already have a sell order today. Skipping.")
        return

    weekly_sells = get_weekly_sell_count(client, account_hash, SYMBOL)
    print(f"  Weekly sells: {weekly_sells} / {WEEKLY_SELL_LIMIT}")

    if weekly_sells >= WEEKLY_SELL_LIMIT:
        print(f"  Weekly sell limit reached. Skipping.")
        return

    account_data = get_account_data(client, account_hash)
    shares_held = get_shares_held(account_data, SYMBOL)
    shares_to_sell = min(shares_wanted, int(shares_held))
    print(f"  Shares held: {int(shares_held)}  Want to sell: {shares_wanted}  Will sell: {shares_to_sell}")

    if shares_to_sell < 1:
        print(f"  No {SYMBOL} shares to sell. Skipping.")
        return

    place_sell_order(client, account_hash, SYMBOL, shares_to_sell, last, dry_run)


def backtest(client, days):
    """Run strategy against historical daily OHLC data."""
    end = datetime.now()
    start = end - timedelta(days=days)

    print(f"[backtest] Fetching {SYMBOL} price history for {days} days...")
    resp = client.get_price_history_every_day(SYMBOL, start_datetime=start, end_datetime=end)
    resp.raise_for_status()
    candles = resp.json().get("candles", [])
    if not candles:
        print("[backtest] No historical data returned.")
        return

    cash = MIN_CASH
    shares = 0
    trades = []
    weekly_buy_spend = 0.0
    weekly_sell_count = 0
    last_week_num = None

    print(f"[backtest] Starting with ${cash:.2f} cash, 0 shares.")
    print(f"[backtest] Simulating {len(candles)} trading days...\n")

    for candle in candles:
        dt = datetime.fromtimestamp(candle["datetime"] / 1000)
        open_price = candle["open"]
        low = candle["low"]
        high = candle["high"]

        # Reset weekly counters each new week
        week_num = dt.isocalendar()[1]
        if last_week_num is not None and week_num != last_week_num:
            weekly_buy_spend = 0.0
            weekly_sell_count = 0
        last_week_num = week_num

        # Check for buy: use low as worst-case intraday price
        pct_low = (low - open_price) / open_price if open_price else 0.0
        if pct_low <= DIP_THRESHOLD:
            buy_price = open_price * (1 + DIP_THRESHOLD)  # approximate trigger price
            buy_shares = math.floor(BUY_AMOUNT / buy_price)
            if (buy_shares >= 1
                    and cash >= MIN_CASH
                    and weekly_buy_spend + BUY_AMOUNT <= WEEKLY_BUY_LIMIT):
                cost = buy_shares * buy_price
                cash -= cost
                shares += buy_shares
                weekly_buy_spend += cost
                trades.append((dt.date(), "BUY", buy_shares, buy_price, cash, shares))
                print(f"  {dt.date()}  BUY  {buy_shares} @ ${buy_price:.2f}  "
                      f"cash=${cash:.2f}  shares={shares}")

        # Check for sell: use high as best-case intraday price
        pct_high = (high - open_price) / open_price if open_price else 0.0
        if pct_high >= SURGE_THRESHOLD and shares > 0:
            sell_price = open_price * (1 + SURGE_THRESHOLD)  # approximate trigger price
            sell_shares = min(math.floor(SELL_AMOUNT / sell_price), shares)
            if sell_shares >= 1 and weekly_sell_count < WEEKLY_SELL_LIMIT:
                proceeds = sell_shares * sell_price
                cash += proceeds
                shares -= sell_shares
                weekly_sell_count += 1
                trades.append((dt.date(), "SELL", sell_shares, sell_price, cash, shares))
                print(f"  {dt.date()}  SELL {sell_shares} @ ${sell_price:.2f}  "
                      f"cash=${cash:.2f}  shares={shares}")

    # Final summary
    final_price = candles[-1]["close"]
    portfolio_value = cash + shares * final_price
    initial_value = MIN_CASH
    total_return = (portfolio_value - initial_value) / initial_value

    buys = [t for t in trades if t[1] == "BUY"]
    sells = [t for t in trades if t[1] == "SELL"]

    print(f"\n{'=' * 60}")
    start_date = datetime.fromtimestamp(candles[0]["datetime"] / 1000).date()
    end_date = datetime.fromtimestamp(candles[-1]["datetime"] / 1000).date()
    print(f"  Backtest Results: {start_date} — {end_date}")
    print(f"  {SYMBOL} final close: ${final_price:.2f}")
    print(f"  Trades: {len(buys)} buys, {len(sells)} sells")
    print(f"  Final cash:   ${cash:.2f}")
    print(f"  Final shares: {shares} (worth ${shares * final_price:.2f})")
    print(f"  Portfolio:    ${portfolio_value:.2f}")
    print(f"  Total return: {total_return:+.2%}")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(description="SOXL trading agent")
    parser.add_argument("--show-accounts", action="store_true",
                        help="Show linked accounts and exit")
    parser.add_argument("--once", action="store_true",
                        help="Run one check cycle and exit (no loop)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run checks against live data but do not place orders")
    parser.add_argument("--backtest", type=int, metavar="DAYS",
                        help="Backtest strategy against N days of historical data")
    args = parser.parse_args()

    load_env()
    lock_fd = acquire_lock()
    client = get_client()

    if args.show_accounts:
        show_accounts(client)
        return

    if args.backtest:
        backtest(client, args.backtest)
        return

    account_hash = get_account_hash(client)

    if args.once or args.dry_run:
        check_and_trade(client, account_hash, dry_run=args.dry_run)
        return

    print(f"[agent] Starting SOXL agent. Checking every {CHECK_INTERVAL_SECONDS // 60} minutes.")
    print(f"[agent] Buy ${BUY_AMOUNT:.0f} if down {DIP_THRESHOLD:.0%} (max ${WEEKLY_BUY_LIMIT:.0f}/wk), "
          f"sell ${SELL_AMOUNT:.0f} if up +{SURGE_THRESHOLD:.0%} (max {WEEKLY_SELL_LIMIT}/wk).")

    while True:
        try:
            check_and_trade(client, account_hash)
        except Exception as e:
            print(f"[error] {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
