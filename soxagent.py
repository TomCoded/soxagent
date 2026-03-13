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
from schwab.orders.equities import equity_buy_market, equity_sell_market

SYMBOL = "SOXL"
DIP_THRESHOLD = -0.10  # -10%
SURGE_THRESHOLD = 0.10  # +10%
MIN_CASH = 250.0
BUY_AMOUNT = 200.0
SELL_AMOUNT = 200.0
WEEKLY_BUY_LIMIT = 400.0
WEEKLY_SELL_LIMIT = 2  # max sell orders per week
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


def get_cash_balance(client, account_hash):
    """Return available cash in the account."""
    resp = client.get_account(account_hash)
    resp.raise_for_status()
    data = resp.json()
    balances = data["securitiesAccount"]["currentBalances"]
    cash = balances.get("cashBalance", balances.get("availableFunds", 0.0))
    return float(cash)


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


def place_sell_order(client, account_hash, symbol, dollar_amount, current_price):
    """Place a market sell order for the given dollar amount of shares."""
    shares = math.floor(dollar_amount / current_price)
    if shares < 1:
        print(f"[order] Price ${current_price:.2f} too high to sell even 1 share for ${dollar_amount:.2f}.")
        return False

    order = equity_sell_market(symbol, shares)
    print(f"[order] Placing market SELL for {shares} shares of {symbol} (~${shares * current_price:.2f})...")
    resp = client.place_order(account_hash, order)
    resp.raise_for_status()
    print(f"[order] Order placed successfully. Status: {resp.status_code}")
    return True


def place_buy_order(client, account_hash, symbol, dollar_amount, current_price):
    """Place a market buy order for the given dollar amount of shares."""
    shares = math.floor(dollar_amount / current_price)
    if shares < 1:
        print(f"[order] Price ${current_price:.2f} too high to buy even 1 share with ${dollar_amount:.2f}.")
        return False

    order = equity_buy_market(symbol, shares)
    print(f"[order] Placing market BUY for {shares} shares of {symbol} (~${shares * current_price:.2f})...")
    resp = client.place_order(account_hash, order)
    resp.raise_for_status()
    print(f"[order] Order placed successfully. Status: {resp.status_code}")
    return True


def check_and_trade(client, account_hash):
    """Run one check cycle: get quote, evaluate conditions, maybe buy or sell."""
    now = datetime.now()
    print(f"\n[{now.strftime('%H:%M:%S')}] Checking {SYMBOL}...")

    # Get current quote
    last, open_price, pct_change = get_quote(client, SYMBOL)
    print(f"  Open: ${open_price:.2f}  Last: ${last:.2f}  Change: {pct_change:+.2%}")

    if pct_change <= DIP_THRESHOLD:
        check_buy(client, account_hash, last, pct_change)
    elif pct_change >= SURGE_THRESHOLD:
        check_sell(client, account_hash, last, pct_change)
    else:
        print(f"  No action (buy threshold: {DIP_THRESHOLD:.0%}, sell threshold: +{SURGE_THRESHOLD:.0%}).")


def check_buy(client, account_hash, last, pct_change):
    """Evaluate buy conditions and place order if met."""
    print(f"  Down {pct_change:+.2%} — meets {DIP_THRESHOLD:.0%} buy threshold!")

    if has_orders_today(client, account_hash, SYMBOL, "BUY"):
        print("  Already have a buy order today. Skipping.")
        return

    weekly_spend = get_weekly_spend(client, account_hash, SYMBOL)
    remaining = WEEKLY_BUY_LIMIT - weekly_spend
    print(f"  Weekly buy spend: ${weekly_spend:.2f} / ${WEEKLY_BUY_LIMIT:.2f} (${remaining:.2f} remaining)")

    if remaining < BUY_AMOUNT:
        print(f"  Weekly buy limit would be exceeded. Skipping.")
        return

    cash = get_cash_balance(client, account_hash)
    print(f"  Cash available: ${cash:.2f}")

    if cash < MIN_CASH:
        print(f"  Insufficient cash (need ${MIN_CASH:.2f}). Skipping.")
        return

    place_buy_order(client, account_hash, SYMBOL, BUY_AMOUNT, last)


def check_sell(client, account_hash, last, pct_change):
    """Evaluate sell conditions and place order if met."""
    print(f"  Up {pct_change:+.2%} — meets +{SURGE_THRESHOLD:.0%} sell threshold!")

    if has_orders_today(client, account_hash, SYMBOL, "SELL"):
        print("  Already have a sell order today. Skipping.")
        return

    weekly_sells = get_weekly_sell_count(client, account_hash, SYMBOL)
    print(f"  Weekly sells: {weekly_sells} / {WEEKLY_SELL_LIMIT}")

    if weekly_sells >= WEEKLY_SELL_LIMIT:
        print(f"  Weekly sell limit reached. Skipping.")
        return

    place_sell_order(client, account_hash, SYMBOL, SELL_AMOUNT, last)


def main():
    parser = argparse.ArgumentParser(description="SOXL dip-buying agent")
    parser.add_argument("--show-accounts", action="store_true",
                        help="Show linked accounts and exit")
    parser.add_argument("--once", action="store_true",
                        help="Run one check cycle and exit (no loop)")
    args = parser.parse_args()

    load_env()
    lock_fd = acquire_lock()
    client = get_client()

    if args.show_accounts:
        show_accounts(client)
        return

    account_hash = get_account_hash(client)

    if args.once:
        check_and_trade(client, account_hash)
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
