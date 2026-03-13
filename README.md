# SOXAgent

Automated SOXL dip-buying agent using the Schwab Trader API.

Checks SOXL price every 15 minutes during the trading day. If all three conditions are met, it buys $200 of SOXL:

1. SOXL is down 10%+ from today's open price
2. No trades have been placed today
3. At least $250 cash is available in the account

## Prerequisites

- Python 3.11+
- A Schwab brokerage account
- A registered app at [developer.schwab.com](https://developer.schwab.com)
  - App status must be "Ready for use" (takes a few days after creation)
  - Set callback URL to `https://127.0.0.1`

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Schwab app credentials
```

## First Run (OAuth Authentication)

The first time you run the agent, it will open a browser for Schwab OAuth login:

```bash
python soxagent.py --once
```

1. Log in with your Schwab **brokerage** credentials
2. Authorize the app
3. Copy the full redirect URL from your browser and paste it into the terminal
4. A `token.json` file will be saved for future use

The access token lasts 30 minutes (auto-refreshed). The refresh token lasts **7 days** -- you must re-authenticate after that.

## Usage

```bash
# Run continuously (checks every 15 minutes)
python soxagent.py

# Run a single check and exit
python soxagent.py --once

# List linked accounts and their hashes
python soxagent.py --show-accounts
```

## Configuration

All settings are in `.env`:

| Variable | Description |
|---|---|
| `SCHWAB_APP_KEY` | App Key from developer.schwab.com |
| `SCHWAB_APP_SECRET` | App Secret from developer.schwab.com |
| `SCHWAB_CALLBACK_URL` | OAuth callback URL (default: `https://127.0.0.1`) |
| `SCHWAB_TOKEN_PATH` | Path to save OAuth token (default: `./token.json`) |
| `SCHWAB_ACCOUNT_HASH` | Account hash (optional; uses first account if blank) |

Trading parameters are constants in `soxagent.py` (top of file):

| Constant | Default | Description |
|---|---|---|
| `SYMBOL` | `SOXL` | Ticker to monitor |
| `DIP_THRESHOLD` | `-0.10` | Buy trigger (10% drop from open) |
| `MIN_CASH` | `250.0` | Minimum cash required in account |
| `BUY_AMOUNT` | `200.0` | Dollar amount to purchase |
| `CHECK_INTERVAL_SECONDS` | `900` | Check frequency (15 minutes) |
