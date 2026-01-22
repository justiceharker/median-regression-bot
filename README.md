# Median Regression Bot

Automated trading bot for Kalshi using median-reversion strategy. Tracks rolling median price and takes profits when price deviates above median, with time-based risk management.

## Features
- **Median-Reversion Strategy:** Tracks a rolling window of prices and executes sells when price exceeds median + deviation threshold.
- **Environment-Configurable:** All strategy params controlled via env vars (window size, thresholds, hold times).
- **Paper Trading Mode:** Test the strategy without live orders.
- **Rich Dashboard:** Real-time position tracking with profit/loss and deviation metrics.
- **Logging:** Trade execution history saved to CSV.

## Quick Start

### 1. Setup Python Environment
```powershell
python -m venv .venv
& .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure Kalshi Credentials
Create a `.env` file (or export as environment variables):
```
KALSHI_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY_PATH=kalshi_key.pem
```

**IMPORTANT:** Never commit `kalshi_key.pem` or API keys to git. The `.gitignore` excludes these automatically.

### 3. Run in Paper Trading Mode (Test)
```powershell
$env:PAPER_TRADING="true"
python median_regression.py
```

### 4. Run Live (with real orders)
```powershell
python median_regression.py
```

## Strategy Parameters (Environment Variables)

Customize behavior by setting these before running:

| Variable | Default | Description |
|----------|---------|-------------|
| `MR_WINDOW` | 15 | Rolling window size (price samples) |
| `MR_THRESHOLD` | 5.0 | Deviation % above median to trigger sell |
| `MR_MAX_HOLD` | 3600 | Max hold time in seconds (1 hour) |
| `MR_REFRESH` | 2 | Update frequency (seconds) |
| `PAPER_TRADING` | false | Set to `true` to test without executing |
| `KALSHI_LOG_FILE` | trading_log.csv | Where to save trade logs |

Example:
```powershell
$env:MR_WINDOW="20"
$env:MR_THRESHOLD="3.0"
$env:PAPER_TRADING="true"
python median_regression.py
```

## Files
- `median_regression.py` — Main trading bot (median-reversion strategy).
- `trading_log.csv` — Trade execution history (auto-generated, ignored by git).
- `kalshi_key.pem` — Kalshi API private key (not in git, add locally).

## License
MIT — See [LICENSE](LICENSE)
