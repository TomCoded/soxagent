#!/usr/bin/env python3
"""
SOXAgent - Automated SOXL dip-buying agent using the Schwab Trader API.

Checks SOXL price every 15 minutes. If SOXL is down 10%+ from today's open,
no trades have been placed today, and there is at least $250 cash in the
account, it buys $200 worth of SOXL.
"""

import argparse
import json
import math
import os
import time
from datetime import datetime, date

import schwab
from schwab.orders.equities import equity_buy_market

SYMBOL = "SOXL"
DIP_THRESHOLD = -0.10  # -10%
MIN_CASH = 250.0
BUY_AMOUNT = 200.0
CHECK_INTERVAL_SECONDS = 15 * 60  # 15 minutes


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


def has_orders_today(client, account_hash):
    """Check if any orders were placed today."""
    today = datetime.now()
    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    resp = client.get_orders_for_account(
        account_hash,
        from_entered_datetime=start,
        to_entered_datetime=today,
    )
    resp.raise_for_status()
    orders = resp.json()
    return len(orders) > 0


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
    """Run one check cycle: get quote, evaluate conditions, maybe trade."""
    now = datetime.now()
    print(f"\n[{now.strftime('%H:%M:%S')}] Checking {SYMBOL}...")

    # Get current quote
    last, open_price, pct_change = get_quote(client, SYMBOL)
    print(f"  Open: ${open_price:.2f}  Last: ${last:.2f}  Change: {pct_change:+.2%}")

    if pct_change > DIP_THRESHOLD:
        print(f"  Not down enough (threshold: {DIP_THRESHOLD:.0%}). Skipping.")
        return

    print(f"  Down {pct_change:+.2%} — meets {DIP_THRESHOLD:.0%} threshold!")

    # Check if we already traded today
    if has_orders_today(client, account_hash):
        print("  Already have orders today. Skipping.")
        return

    # Check cash balance
    cash = get_cash_balance(client, account_hash)
    print(f"  Cash available: ${cash:.2f}")

    if cash < MIN_CASH:
        print(f"  Insufficient cash (need ${MIN_CASH:.2f}). Skipping.")
        return

    # Place the buy
    place_buy_order(client, account_hash, SYMBOL, BUY_AMOUNT, last)


def main():
    parser = argparse.ArgumentParser(description="SOXL dip-buying agent")
    parser.add_argument("--show-accounts", action="store_true",
                        help="Show linked accounts and exit")
    parser.add_argument("--once", action="store_true",
                        help="Run one check cycle and exit (no loop)")
    args = parser.parse_args()

    load_env()
    client = get_client()

    if args.show_accounts:
        show_accounts(client)
        return

    account_hash = get_account_hash(client)

    if args.once:
        check_and_trade(client, account_hash)
        return

    print(f"[agent] Starting SOXL agent. Checking every {CHECK_INTERVAL_SECONDS // 60} minutes.")
    print(f"[agent] Buy ${BUY_AMOUNT:.0f} of {SYMBOL} if down {DIP_THRESHOLD:.0%}, "
          f"cash >= ${MIN_CASH:.0f}, no trades today.")

    while True:
        try:
            check_and_trade(client, account_hash)
        except Exception as e:
            print(f"[error] {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
