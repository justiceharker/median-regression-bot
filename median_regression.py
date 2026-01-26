import time
import os
import csv
import datetime
import threading
from collections import deque
from statistics import median, stdev
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.console import Group

# Load environment variables from .env file
load_dotenv()

# Configuration via environment variables (safer than hardcoding)
KEY_ID = os.getenv("KALSHI_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_key.pem")
LOG_FILE = os.getenv("KALSHI_LOG_FILE", "trading_log.csv")

# Strategy parameters
ROLLING_WINDOW = int(os.getenv("MR_WINDOW", "15"))
DEVIATION_THRESHOLD_PCT = float(os.getenv("MR_THRESHOLD", "5.0"))  # percent (base)
MAX_HOLD_SECONDS = int(os.getenv("MR_MAX_HOLD", str(60 * 60)))  # 1 hour
REFRESH_RATE = float(os.getenv("MR_REFRESH", "2"))

# Liquidity filtering parameters
MIN_OPEN_INTEREST = int(os.getenv("MIN_OPEN_INTEREST", "100"))  # min shares
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "2.0"))  # max spread %

# Entry logic parameters
HOURS_BEFORE_CLOSE = int(os.getenv("HOURS_BEFORE_CLOSE", "2"))  # don't enter this close to close

# Safety parameters
MIN_HOLD_TIME = 30
STOP_LOSS_PERCENT = 0.10  # 10% loss triggers stop
STOP_LOSS_FLOOR = 0.35    # Absolute floor price
MAX_LOSS_PER_TRADE = 0.12
TIME_BASED_STOP_LOSS = 2700  # 45 min
BREAK_EVEN_TIMER = 1800      # 30 min

console = Console(force_terminal=True, legacy_windows=False, width=160)

# Global flag for manual sell trigger
manual_sell_requested = False
manual_sell_ticker = None

def listen_for_input():
    """Listen for keyboard commands: 's' to sell, 'c' to cancel orders, 'q' to quit."""
    global manual_sell_requested, manual_sell_ticker
    import sys
    import select
    
    console.print("[dim]Keyboard shortcuts: s=sell all, c=cancel orders, q=quit[/dim]")
    
    # On Windows, use a simpler approach
    if sys.platform == 'win32':
        import msvcrt
        while True:
            try:
                if msvcrt.kbhit():
                    key = msvcrt.getch().decode('utf-8').lower()
                    if key == 's':
                        manual_sell_requested = True
                        console.print("[yellow]>> Manual sell requested for all positions[/yellow]")
                    elif key == 'c':
                        # Cancel all open orders
                        open_orders = get_all_open_orders()
                        canceled = 0
                        for order in open_orders:
                            order_id = getattr(order, 'order_id', None)
                            if order_id and cancel_order(order_id):
                                canceled += 1
                        console.print(f"[yellow]X Canceled {canceled}/{len(open_orders)} orders[/yellow]")
                    elif key == 'q':
                        console.print("[yellow]Exiting...[/yellow]")
                        break
                time.sleep(0.1)
            except Exception as e:
                time.sleep(0.1)
    else:
        # Unix/Linux approach
        while True:
            try:
                user_input = input().strip().lower()
                if user_input == 's':
                    manual_sell_requested = True
                    console.print("[yellow]>> Manual sell requested for all positions[/yellow]")
                elif user_input == 'c':
                    open_orders = get_all_open_orders()
                    canceled = 0
                    for order in open_orders:
                        order_id = getattr(order, 'order_id', None)
                        if order_id and cancel_order(order_id):
                            canceled += 1
                    console.print(f"[yellow]X Canceled {canceled}/{len(open_orders)} orders[/yellow]")
                elif user_input == 'q':
                    console.print("[yellow]Exiting...[/yellow]")
                    break
            except EOFError:
                break
            except:
                time.sleep(0.1)

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
    console.print("[green]OK Kalshi client initialized[/green]")
except Exception as e:
    console.print(f"[yellow]Warning: Kalshi client not configured: {e}[/yellow]")


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


def is_market_liquid(market, yes_bid, yes_ask):
    """Check if market meets liquidity requirements."""
    try:
        open_interest = int(getattr(market, 'open_interest', 0) or 0)
        if open_interest < MIN_OPEN_INTEREST:
            return False
        
        # Check spread %
        if yes_bid > 0 and yes_ask > 0:
            spread_pct = abs(yes_ask - yes_bid) / yes_bid * 100
            if spread_pct > MAX_SPREAD_PCT:
                return False
        
        return True
    except:
        return False


def is_market_active_for_entry(market):
    """Check if market is suitable for new entries (not too close to close)."""
    try:
        # Check if market has a close time
        close_time_str = getattr(market, 'close_time', None)
        if not close_time_str:
            return True
        
        # Parse close time (ISO format)
        close_time = datetime.datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
        now = datetime.datetime.now(datetime.timezone.utc)
        time_to_close = (close_time - now).total_seconds() / 3600  # hours
        
        # Don't enter if too close to close
        if time_to_close < HOURS_BEFORE_CLOSE:
            return False
        
        return True
    except:
        return True  # If we can't determine, allow entry


def calculate_dynamic_threshold(prices):
    """Calculate volatility-based threshold adjustment."""
    if len(prices) < 3:
        return DEVIATION_THRESHOLD_PCT
    
    try:
        # Calculate coefficient of variation (volatility)
        price_list = list(prices)
        
        mean_price = sum(price_list) / len(price_list)
        volatility = stdev(price_list) / mean_price if mean_price > 0 else 0
        
        # Adjust threshold: higher volatility = higher threshold needed
        volatility_pct = volatility * 100
        adjusted_threshold = DEVIATION_THRESHOLD_PCT * (1 + (volatility_pct / 100) * 1.0)
        
        return adjusted_threshold
    except:
        return DEVIATION_THRESHOLD_PCT


def log_trade(ticker, title, entry, exit_price, pnl_pct, reason):
    """Saves trade data to CSV."""
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "Ticker", "Event", "Entry", "Exit", "PnL%", "Reason"])
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        writer.writerow([timestamp, ticker, title, f"${entry:.2f}", f"${exit_price:.2f}", f"{pnl_pct:.1f}%", reason])


def log_new_position(ticker, title, entry, shares):
    """Logs when a new position is detected."""
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "Ticker", "Event", "Entry", "Exit", "PnL%", "Reason"])
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        writer.writerow([timestamp, ticker, title, f"${entry:.2f}", "---", "0.0%", f"NEW POSITION ({shares} shares)"])
    
    console.print(f"\n[bold green]NEW POSITION DETECTED![/bold green]")
    console.print(f"[cyan]{title}[/cyan]")
    console.print(f"[white]Entry: ${entry:.2f} | Shares: {shares}[/white]")
    console.print(f"[dim]ker: {ticker}[/dim]\n")


def execute_order(ticker, shares, reason, action="sell"):
    """Executes live order for trading with robust parameters.
    
    For CLOSING positions:
    - Long (positive shares): Sell YES at bid price
    - Short (negative shares): Buy YES at ask price
    
    For OPENING positions:
    - Buy: Buy YES at ask price
    - Sell/Short: Sell YES at bid price (creates short)
    """
    if client is None:
        console.print(f"[red]X No Kalshi client available[/red]")
        return False
    try:
        # Get market data
        market = client.get_market(ticker).market
        
        if action == "sell":
            # Sell YES shares at the bid price
            yes_price = market.yes_bid_dollars
            
            order = client.create_order(
                ticker=ticker,
                side="yes",
                action="sell",
                count=shares,
                type="limit",
                yes_price_dollars=yes_price
            )
            action_str = "SELL"
        else:
            # Buy YES shares at the ask price
            yes_price = market.yes_ask_dollars
            
            order = client.create_order(
                ticker=ticker,
                side="yes",
                action="buy",
                count=shares,
                type="limit",
                yes_price_dollars=yes_price
            )
            action_str = "BUY"
        
        # Log successful order
        order_id = getattr(order, 'order_id', 'UNKNOWN')
        console.print(f"[green]LIVE {action_str} {ticker} {shares} @ ${yes_price} — {reason}[/green]")
        
        # Log to file
        with open("successful_orders.log", "a") as f:
            f.write(f"{datetime.datetime.now()} - Order ID: {order_id}, Ticker: {ticker}, Shares: {shares}, Action: {action_str}, Reason: {reason}\n")
        
        return True
    except Exception as e:
        # Log error silently to avoid spamming console
        import traceback
        error_msg = str(e)
        with open("order_errors.log", "a") as f:
            f.write(f"{datetime.datetime.now()} - Ticker: {ticker}, Action: {reason}\n")
            f.write(f"Error: {error_msg}\n")
            f.write(traceback.format_exc() + "\n\n")
        # Don't print to console to avoid disrupting Live display
        return False


def calculate_stop_loss(entry, current_bid):
    """Calculate stop loss with percentage and floor."""
    percent_stop = entry * (1 - STOP_LOSS_PERCENT)
    return max(percent_stop, STOP_LOSS_FLOOR)


def get_account_balance():
    """Fetch account balance from Kalshi."""
    try:
        if client is None:
            return None
        portfolio = client.get_portfolio()
        balance_cents = getattr(portfolio, 'cash_balance', 0)
        return float(balance_cents) / 100  # Convert cents to dollars
    except:
        return None


def get_all_open_orders():
    """Fetch all open orders."""
    try:
        if client is None:
            return []
        resp = client.get_orders(status="open")
        return getattr(resp, 'orders', [])
    except:
        return []


def cancel_order(order_id):
    """Cancel an open order by ID."""
    try:
        if client is None:
            return False
        client.delete_order(order_id=order_id)
        return True
    except:
        return False


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
    """Creates a detailed Rich Table dashboard with comprehensive market statistics."""
    all_pnl, win_rate = get_stats()
    account_balance = get_account_balance()
    open_orders = get_all_open_orders()
    
    # Get all markets to identify pending outcomes
    pending_markets = []
    try:
        if client:
            markets = client.get_markets()
            for market in markets:
                if hasattr(market, 'status') and market.status == 'PENDING':
                    pending_markets.append(market)
    except:
        pass
    
    # Dynamic color based on performance
    if all_pnl >= 20:
        p_color = "bold green"
        perf_emoji = "^"
    elif all_pnl >= 10:
        p_color = "green"
        perf_emoji = "+"
    elif all_pnl >= 0:
        p_color = "green"
        perf_emoji = "OK"
    elif all_pnl >= -10:
        p_color = "yellow"
        perf_emoji = "!"
    else:
        p_color = "red"
        perf_emoji = "v"
    
    total_trades = len(rows)
    profitable = sum(1 for r in rows if r['pnl'] > 0)
    
    # Build comprehensive header
    balance_str = f"${account_balance:.2f}" if account_balance else "N/A"
    orders_str = f"Resting Orders: {len(open_orders)} | Pending Markets: {len(pending_markets)}"
    
    stats_header = f"[cyan bold]LIVE[/cyan bold] | PnL: [{p_color}]{all_pnl:+.2f}%[/{p_color}] | Win: [cyan]{win_rate:.1f}%[/cyan] | Profit: [green]{profitable}[/green]/[dim]{total_trades}[/dim] | Bal: {balance_str} | {orders_str}"
    
    table = Table(
        title="MEDIAN REGRESSION BOT - ACTIVE POSITIONS",
        title_style="bold white on blue",
        border_style="bright_blue",
        header_style="bold cyan",
        show_lines=False,
        expand=False,
        padding=(0, 1)
    )
    
    table.add_column("Market", style="bold cyan", width=26)
    table.add_column("Entry $", justify="right", style="dim white", width=7)
    table.add_column("Median $", justify="right", style="cyan", width=8)
    table.add_column("Current $", justify="right", style="bold white", width=9)
    table.add_column("Chart", justify="center", width=13)
    table.add_column("Dev%", justify="right", width=7)
    table.add_column("PnL%", justify="right", width=7)
    table.add_column("Spread", justify="right", style="yellow", width=9)
    table.add_column("Hold(m)", justify="right", style="dim", width=7)
    table.add_column("Status", justify="center", width=14)
    
    for r in rows:
        pnl_color = "bold green" if r['pnl'] >= 10 else ("green" if r['pnl'] > 0 else "red")
        dev_color = "bold yellow" if abs(r['dev']) >= DEVIATION_THRESHOLD_PCT else "cyan"
        
        # Spread display
        spread = r.get('spread', 0)
        bid = r.get('bid', 0)
        ask = r.get('ask', 0)
        spread_str = f"${bid:.2f}-{ask:.2f}" if bid > 0 and ask > 0 else "N/A"
        
        table.add_row(
            f"{r['title'][:26]}",
            f"${r['entry']:.2f}",
            f"${r['median']:.2f}",
            f"${r['now']:.2f}",
            r['sparkline'],
            f"[{dev_color}]{r['dev']:+.1f}%[/{dev_color}]",
            f"[{pnl_color}]{r['pnl']:+.1f}%[/{pnl_color}]",
            spread_str,
            f"{r['hold_min']:.1f}",
            r['status']
        )
    
    # Build output with positions table and additional sections
    output = Panel(table, title=stats_header, border_style="blue", padding=(0, 1))
    tables_list = [output]
    
    # Add resting orders section if there are any
    if open_orders and len(open_orders) > 0:
        orders_table = Table(
            title="RESTING ORDERS",
            title_style="bold white on yellow",
            border_style="bright_yellow",
            header_style="bold yellow",
            show_lines=False,
            expand=False,
            padding=(0, 1)
        )
        
        orders_table.add_column("Market", style="bold yellow", width=26)
        orders_table.add_column("Action", justify="center", style="cyan", width=8)
        orders_table.add_column("Shares", justify="right", width=7)
        orders_table.add_column("Price $", justify="right", width=8)
        orders_table.add_column("Order ID", style="dim", width=20)
        
        for order in open_orders[:10]:  # Show max 10 orders
            try:
                ticker = getattr(order, 'ticker', 'N/A')
                action = getattr(order, 'action', 'N/A')
                side = getattr(order, 'side', 'YES')
                quantity = getattr(order, 'quantity', 0)
                yes_price = getattr(order, 'yes_price_dollars', 0)
                no_price = getattr(order, 'no_price_dollars', 0)
                order_id = getattr(order, 'order_id', 'N/A')[:8] + "..." if hasattr(order, 'order_id') else "N/A"
                
                # Determine display price
                display_price = yes_price if yes_price > 0 else no_price
                action_display = f"{action.upper()}"
                
                orders_table.add_row(
                    f"{ticker[:26]}",
                    action_display,
                    f"{quantity}",
                    f"${display_price:.2f}" if display_price > 0 else "N/A",
                    order_id
                )
            except Exception as e:
                continue
        
        tables_list.append(orders_table)
    
    # Add outcome pending section if there are any
    if pending_markets and len(pending_markets) > 0:
        pending_table = Table(
            title="OUTCOME PENDING",
            title_style="bold white on magenta",
            border_style="bright_magenta",
            header_style="bold magenta",
            show_lines=False,
            expand=False,
            padding=(0, 1)
        )
        
        pending_table.add_column("Event", style="bold magenta", width=30)
        pending_table.add_column("Close Time", justify="center", style="cyan", width=16)
        pending_table.add_column("Yes Price", justify="right", width=8)
        
        for market in pending_markets[:15]:  # Show max 15 pending markets
            try:
                title = getattr(market, 'title', 'N/A')[:30]
                close_date = getattr(market, 'close_date', None)
                yes_bid = getattr(market, 'yes_bid_dollars', 0)
                yes_ask = getattr(market, 'yes_ask_dollars', 0)
                
                # Format close time
                if close_date:
                    try:
                        close_time = datetime.datetime.fromisoformat(close_date.replace('Z', '+00:00'))
                        time_str = close_time.strftime("%m/%d %H:%M")
                    except:
                        time_str = "N/A"
                else:
                    time_str = "N/A"
                
                # Average price
                avg_price = (yes_bid + yes_ask) / 2 if yes_bid > 0 and yes_ask > 0 else 0
                
                pending_table.add_row(
                    title,
                    time_str,
                    f"${avg_price:.2f}" if avg_price > 0 else "N/A"
                )
            except Exception as e:
                continue
        
        tables_list.append(pending_table)
    
    # Combine all tables
    if len(tables_list) > 1:
        return Group(*tables_list)
    
    return output


def main_loop():
    """Main trading loop with robust position tracking."""
    global manual_sell_requested, manual_sell_ticker
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
                # Only use market_positions for tracking - event_positions have different ticker format
                all_pos = getattr(resp, 'market_positions', []) or []
                now = time.time()
                
                for pos in all_pos:
                    shares = abs(int(getattr(pos, 'position', 0)))
                    
                    # Skip closed positions
                    if shares <= 0:
                        continue
                    
                    ticker = getattr(pos, 'ticker', getattr(pos, 'event_ticker', 'Unknown'))
                    market = client.get_market(ticker).market
                    current = float(market.yes_bid_dollars)
                    yes_ask = float(getattr(market, 'yes_ask_dollars', current))
                    cost = getattr(pos, 'market_exposure', getattr(pos, 'total_cost', 0))
                    entry = (cost / shares / 100) if shares > 0 else 0  # cost is in cents
                    
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
                    
                    # Calculate dynamic threshold based on volatility
                    dynamic_threshold = calculate_dynamic_threshold(list(price_hist[ticker]))
                    
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
                        # Only log as "new" if meets entry criteria, but still track it
                        if is_market_active_for_entry(market) and is_market_liquid(market, current, yes_ask):
                            known_positions[position_key] = True
                            log_new_position(ticker, market.title, entry, shares)
                        else:
                            # Mark as known to prevent re-logging, even if doesn't meet entry criteria
                            known_positions[position_key] = True
                    
                    # Median reversion sell logic
                    sold = False
                    reason = None
                    
                    # Manual sell override
                    if manual_sell_requested and position_key not in sold_positions:
                        reason = "Manual sell triggered"
                        if execute_order(ticker, shares, reason, action="sell"):
                            log_trade(ticker, market.title, entry, current, pnl, reason)
                            sold_positions.add(position_key)
                            sold = True
                    
                    # Automatic median reversion sell logic (if not manually sold)
                    if not sold and position_key not in sold_positions and dev_pct >= dynamic_threshold and pnl > 0:
                        reason = f"Median reversion +{dynamic_threshold:.2f}% deviation"
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
                        # Don't delete from known_positions — keeps it from logging as "new" again
                        continue
                    
                    # Get sparkline
                    spark = get_sparkline(list(price_hist[ticker]))
                    
                    # Calculate bid-ask spread
                    bid = market.yes_bid_dollars if market.yes_bid_dollars else 0
                    ask = market.yes_ask_dollars if market.yes_ask_dollars else 0
                    spread = ask - bid if bid > 0 else 0
                    
                    # Determine status with momentum indicator
                    if abs(dev_pct) >= DEVIATION_THRESHOLD_PCT:
                        status = "[bold yellow]! THRESHOLD[/bold yellow]" if dev_pct < 0 else "[bold green]OK READY[/bold green]"
                    else:
                        status = "[cyan]~ Tracking[/cyan]"
                    
                    rows.append({
                        "ticker": ticker,
                        "title": market.title,
                        "entry": entry,
                        "now": current,
                        "median": med,
                        "dev": dev_pct,
                        "pnl": pnl,
                        "peak": peak,
                        "sparkline": spark,
                        "hold_min": hold_sec / 60.0,
                        "status": status,
                        "spread": spread,
                        "bid": bid,
                        "ask": ask,
                    })

                rows = sorted(rows, key=lambda x: x['pnl'], reverse=True)
                live.update(generate_dashboard(rows))
                
                # Reset manual sell flag after processing
                if manual_sell_requested:
                    manual_sell_requested = False
                
                time.sleep(REFRESH_RATE)

            except KeyboardInterrupt:
                console.print("[yellow]Stopped by user[/yellow]")
                break
            except Exception as e:
                # Log error silently to avoid disrupting Live display
                import traceback
                with open("error.log", "a") as f:
                    f.write(f"{datetime.datetime.now()} - Error: {str(e)}\n")
                    f.write(traceback.format_exc() + "\n")
                time.sleep(1)


if __name__ == "__main__":
    console.print("[cyan]Starting Median Regression Bot[/cyan]")
    console.print(f"[dim]Strategy: Median window={ROLLING_WINDOW}, threshold={DEVIATION_THRESHOLD_PCT}%[/dim]")
    
    # Start input listener thread
    input_thread = threading.Thread(target=listen_for_input, daemon=True)
    input_thread.start()
    
    main_loop()
