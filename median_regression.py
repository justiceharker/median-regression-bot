import time
import os
import csv
import datetime
from collections import deque
from statistics import median
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.table import Table

# Load environment variables from .env file
load_dotenv()

# Configuration via environment variables (safer than hardcoding)
KEY_ID = os.getenv("KALSHI_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_key.pem")
LOG_FILE = os.getenv("KALSHI_LOG_FILE", "trading_log.csv")
PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() in ("1", "true", "yes")

# Strategy parameters
ROLLING_WINDOW = int(os.getenv("MR_WINDOW", "15"))
DEVIATION_THRESHOLD_PCT = float(os.getenv("MR_THRESHOLD", "5.0"))  # percent
MAX_HOLD_SECONDS = int(os.getenv("MR_MAX_HOLD", str(60 * 60)))  # 1 hour
REFRESH_RATE = float(os.getenv("MR_REFRESH", "2"))

# Safety parameters
MIN_HOLD_TIME = 30
STOP_LOSS_PERCENT = 0.10  # 10% loss triggers stop
STOP_LOSS_FLOOR = 0.35    # Absolute floor price
MAX_LOSS_PER_TRADE = 0.12
TIME_BASED_STOP_LOSS = 2700  # 45 min
BREAK_EVEN_TIMER = 1800      # 30 min

console = Console()

# Try to initialize Kalshi client if available; remain tolerant if not running live
client = None
try:
    from kalshi_python_sync import Configuration, KalshiClient
    with open(PRIVATE_KEY_PATH, "r") as f:
        private_key = f.read()
    config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    if KEY_ID:
        config.api_key_id = KEY_ID
    config.private_key_pem = private_key
    client = KalshiClient(config)
    console.print("[green]✓ Kalshi client initialized[/green]")
except Exception as e:
    console.print(f"[yellow]Warning: Kalshi client not configured: {e}[/yellow]")


def get_sport_info(ticker):
    """Assigns icons based on ticker strings."""
    t = ticker.upper()
    icons = {"NBA": "🏀", "NHL": "🏒", "SOC": "⚽", "TEN": "🎾", "NFL": "🏈", "MLB": "⚾", "POL": "🏛️"}
    for key, icon in icons.items():
        if key in t: 
            return icon
    return "💰"


def get_sparkline(prices):
    """Generates a tiny bar graph using Unicode block characters with color."""
    if len(prices) < 2: 
        return " "
    chars = " ▁▂▃▄▅▆▇█"
    min_p, max_p = min(prices), max(prices)
    diff = max_p - min_p
    if diff == 0: 
        return "[dim]▄" * len(prices) + "[/dim]"
    
    line = ""
    for i, p in enumerate(prices):
        idx = int(((p - min_p) / diff) * 8)
        idx = min(idx, 7)
        
        # Color gradient based on trend
        if i < len(prices) - 1:
            if prices[i+1] > p:
                color = "green"
            elif prices[i+1] < p:
                color = "red"
            else:
                color = "yellow"
        else:
            color = "cyan"
        
        line += f"[{color}]{chars[idx]}[/{color}]"
    return line


def get_stats():
    """Calculates win rate and PnL from the log file."""
    total_pnl = 0.0
    wins, total_trades = 0, 0
    if not os.path.isfile(LOG_FILE): 
        return 0.0, 0.0
    with open(LOG_FILE, mode="r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                p_val = float(row['PnL%'].replace('%', ''))
                total_pnl += p_val
                total_trades += 1
                if p_val > 0: 
                    wins += 1
            except: 
                continue
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    return total_pnl, win_rate


def log_trade(ticker, title, entry, exit_price, pnl_pct, reason):
    """Saves trade data to CSV."""
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "Ticker", "Event", "Entry", "Exit", "PnL%", "Reason", "Mode"])
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        mode = "SIMULATED" if PAPER_TRADING else "LIVE"
        writer.writerow([timestamp, ticker, title, f"${entry:.2f}", f"${exit_price:.2f}", f"{pnl_pct:.1f}%", reason, mode])


def log_new_position(ticker, title, entry, shares):
    """Logs when a new position is detected."""
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "Ticker", "Event", "Entry", "Exit", "PnL%", "Reason", "Mode"])
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        mode = "SIMULATED" if PAPER_TRADING else "LIVE"
        writer.writerow([timestamp, ticker, title, f"${entry:.2f}", "---", "0.0%", f"NEW POSITION ({shares} shares)", mode])
    
    console.print(f"\n[bold green]🎉 NEW POSITION DETECTED![/bold green]")
    console.print(f"[cyan]📊 {title}[/cyan]")
    console.print(f"[white]💰 Entry: ${entry:.2f} | Shares: {shares}[/white]")
    console.print(f"[dim]🎫 Ticker: {ticker}[/dim]\n")


def execute_order(ticker, shares, reason, action="sell"):
    """Executes or simulates order based on PAPER_TRADING mode."""
    if PAPER_TRADING or client is None:
        console.print(f"[yellow]📝 SIMULATED {action.upper()} {ticker} {shares} — {reason}[/yellow]")
        return True
    try:
        client.create_order(ticker=ticker, action=action, count=shares, type="market", side="yes")
        console.print(f"[green]✅ LIVE {action.upper()} {ticker} {shares} — {reason}[/green]")
        return True
    except Exception as e:
        console.print(f"[red]❌ Order failed: {e}[/red]")
        return False


def calculate_stop_loss(entry, current_bid):
    """Calculate stop loss with percentage and floor."""
    percent_stop = entry * (1 - STOP_LOSS_PERCENT)
    return max(percent_stop, STOP_LOSS_FLOOR)


def should_execute_stop(ticker, current_bid, entry, hold_time):
    """Multiple safety triggers for risk management."""
    stop_price = calculate_stop_loss(entry, current_bid)
    pnl_percent = ((current_bid - entry) / entry * 100) if entry > 0 else 0
    
    if hold_time < MIN_HOLD_TIME:
        return False, None
    
    # Standard stop loss
    if current_bid <= stop_price:
        return True, f"Stop Loss Hit (${current_bid:.2f} <= ${stop_price:.2f})"
    
    # Emergency exit for big losses
    if pnl_percent <= -MAX_LOSS_PER_TRADE * 100:
        return True, f"Max Loss Exceeded ({pnl_percent:.1f}%)"
    
    # Time-based stop - if losing for 45+ min
    if hold_time >= TIME_BASED_STOP_LOSS and pnl_percent < 0:
        return True, f"Time-Based Stop (Losing for {hold_time/60:.1f} min)"
    
    # Break-even protection - after 30 min, exit if near break-even
    if hold_time >= BREAK_EVEN_TIMER and pnl_percent >= -2 and pnl_percent <= 3:
        return True, f"Break-Even Exit ({pnl_percent:.1f}% PnL)"
    
    return False, None


def generate_dashboard(rows):
    """Creates a detailed Rich Table dashboard with statistics."""
    all_pnl, win_rate = get_stats()
    
    # Dynamic color based on performance
    if all_pnl >= 20:
        p_color = "bold green"
        perf_emoji = "🚀"
    elif all_pnl >= 10:
        p_color = "green"
        perf_emoji = "📈"
    elif all_pnl >= 0:
        p_color = "green"
        perf_emoji = "✅"
    elif all_pnl >= -10:
        p_color = "yellow"
        perf_emoji = "⚠️"
    else:
        p_color = "red"
        perf_emoji = "🔻"
    
    mode_indicator = "[yellow bold]📝 PAPER TRADING[/yellow bold]" if PAPER_TRADING else "[cyan bold]⚡ LIVE MODE[/cyan bold]"
    total_trades = len(rows)
    profitable = sum(1 for r in rows if r['pnl'] > 0)
    
    stats_header = f"{mode_indicator}  |  {perf_emoji} PnL: [{p_color}]{all_pnl:+.1f}%[/{p_color}]  |  Win Rate: [cyan]{win_rate:.1f}%[/cyan]  |  Positions: [green]{profitable}[/green]/[dim]{total_trades}[/dim]"
    
    table = Table(
        title="📊 MEDIAN REGRESSION BOT 📊",
        title_style="bold white on blue",
        border_style="bright_blue",
        header_style="bold cyan",
        show_lines=True,
        expand=True,
        padding=(0, 1)
    )
    
    table.add_column("Market", style="bold cyan", width=25)
    table.add_column("Entry", justify="right", style="white")
    table.add_column("Median", justify="right", style="white")
    table.add_column("Now", justify="right", style="bold white")
    table.add_column("Peak", justify="right", style="dim cyan")
    table.add_column("Dev%", justify="right")
    table.add_column("Chart", justify="center", width=15)
    table.add_column("PnL%", justify="right", width=10)
    table.add_column("Hold", justify="right")
    table.add_column("Status", justify="center", width=18)
    
    for r in rows:
        pnl_color = "bold green" if r['pnl'] >= 10 else ("green" if r['pnl'] > 0 else "red")
        dev_color = "bold yellow" if abs(r['dev']) >= DEVIATION_THRESHOLD_PCT else "cyan"
        hold_min = r['hold_min']
        hold_str = f"{hold_min:.1f}m" if hold_min >= 1 else f"{int(hold_min * 60)}s"
        
        table.add_row(
            f"{get_sport_info(r['ticker'])} {r['title'][:20]}",
            f"${r['entry']:.2f}",
            f"${r['median']:.2f}",
            f"${r['now']:.2f}",
            f"${r['peak']:.2f}",
            f"[{dev_color}]{r['dev']:+.2f}%[/{dev_color}]",
            r['sparkline'],
            f"[{pnl_color}]{r['pnl']:+.1f}%[/{pnl_color}]",
            hold_str,
            r['status']
        )
    
    return Panel(table, title=stats_header, subtitle=f"Updated: {datetime.datetime.now().strftime('%H:%M:%S')}", border_style="blue")


def main_loop():
    """Main trading loop with robust position tracking."""
    price_hist = {}
    entry_times = {}
    highest_prices = {}
    last_prices = {}
    known_positions = {}
    sold_positions = set()  # Track positions that have been sold to prevent duplicates
    
    with Live(generate_dashboard([]), refresh_per_second=1, screen=True) as live:
        while True:
            rows = []
            try:
                if client is None:
                    console.print("[red]No Kalshi client; retrying in 5s...[/red]")
                    time.sleep(5)
                    continue

                resp = client.get_positions()
                all_pos = (getattr(resp, 'market_positions', []) or []) + (getattr(resp, 'event_positions', []) or [])
                now = time.time()
                
                for pos in all_pos:
                    shares = abs(int(getattr(pos, 'position', 0)))
                    if shares <= 0:
                        continue
                    
                    ticker = getattr(pos, 'ticker', getattr(pos, 'event_ticker', 'Unknown'))
                    market = client.get_market(ticker).market
                    current = float(market.yes_bid_dollars)
                    cost = getattr(pos, 'market_exposure', getattr(pos, 'total_cost', 0))
                    entry = (cost / shares) / 100 if cost > 100 else (cost / shares)

                    # Initialize tracking
                    if ticker not in price_hist:
                        price_hist[ticker] = deque(maxlen=ROLLING_WINDOW)
                    if ticker not in entry_times:
                        entry_times[ticker] = now
                    if ticker not in highest_prices:
                        highest_prices[ticker] = current
                    
                    # Update price history
                    price_hist[ticker].append(current)
                    med = median(list(price_hist[ticker])) if len(price_hist[ticker]) >= 3 else current
                    dev_pct = (current - med) / med * 100 if med != 0 else 0.0
                    pnl = ((current - entry) / entry * 100) if entry > 0 else 0.0
                    hold_sec = now - entry_times[ticker]
                    
                    # Track peak
                    if current > highest_prices[ticker]:
                        highest_prices[ticker] = current
                    peak = highest_prices[ticker]
                    
                    # Log new position
                    position_key = f"{ticker}_{shares}"
                    if position_key not in known_positions:
                        known_positions[position_key] = True
                        log_new_position(ticker, market.title, entry, shares)
                    
                    # Median reversion sell logic
                    sold = False
                    reason = None
                    if position_key not in sold_positions and dev_pct >= DEVIATION_THRESHOLD_PCT and pnl > 0:
                        reason = f"Median reversion +{DEVIATION_THRESHOLD_PCT}% deviation"
                        if execute_order(ticker, shares, reason, action="sell"):
                            log_trade(ticker, market.title, entry, current, pnl, reason)
                            sold_positions.add(position_key)
                            sold = True
                    
                    # Safety stops
                    if position_key not in sold_positions:
                        should_stop, stop_reason = should_execute_stop(ticker, current, entry, hold_sec)
                        if should_stop:
                            if execute_order(ticker, shares, stop_reason, action="sell"):
                                log_trade(ticker, market.title, entry, current, pnl, stop_reason)
                                sold_positions.add(position_key)
                                sold = True
                    
                    if sold:
                        if ticker in price_hist:
                            del price_hist[ticker]
                        if ticker in entry_times:
                            del entry_times[ticker]
                        if position_key in known_positions:
                            del known_positions[position_key]
                        continue
                    
                    # Get sparkline
                    spark = get_sparkline(list(price_hist[ticker]))
                    
                    # Determine status
                    if abs(dev_pct) >= DEVIATION_THRESHOLD_PCT:
                        status = "[bold yellow]⚠️ THRESHOLD[/bold yellow]" if dev_pct < 0 else "[bold green]✓ READY[/bold green]"
                    else:
                        status = "[cyan]📡 Tracking[/cyan]"
                    
                    rows.append({
                        "ticker": ticker,
                        "title": market.title[:20],
                        "entry": entry,
                        "now": current,
                        "median": med,
                        "dev": dev_pct,
                        "pnl": pnl,
                        "peak": peak,
                        "sparkline": spark,
                        "hold_min": hold_sec / 60.0,
                        "status": status,
                    })

                rows = sorted(rows, key=lambda x: x['pnl'], reverse=True)
                live.update(generate_dashboard(rows))
                time.sleep(REFRESH_RATE)

            except KeyboardInterrupt:
                console.print("[yellow]Stopped by user[/yellow]")
                break
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
                time.sleep(3)


if __name__ == "__main__":
    console.print("[cyan]Starting Median Regression Bot[/cyan]")
    console.print(f"[dim]Strategy: Median window={ROLLING_WINDOW}, threshold={DEVIATION_THRESHOLD_PCT}%[/dim]")
    main_loop()
