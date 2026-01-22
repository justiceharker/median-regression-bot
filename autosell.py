import time, datetime, csv, os
from kalshi_python_sync import Configuration, KalshiClient
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel

# --- CONFIGURATION ---
KEY_ID = "440e151a-429a-47f1-aebb-933e615f93c2"
PRIVATE_KEY_PATH = "kalshi_key.pem" 
LOG_FILE = "trading_log.csv"

# --- TESTING MODE ---
PAPER_TRADING = False  # Set to True for paper trading
SIMULATED_TRADES_LOG = "simulated_trades.csv"

# --- SMART TRADING RULES (HIGH PROFIT + SAFETY) ---
# Multi-tier profit system: lock in gains progressively
PROFIT_TIER_1 = 1.05        # 5% - Start tracking, loose trailing
PROFIT_TIER_2 = 1.10        # 10% - Tighten trailing stop
PROFIT_TIER_3 = 1.20        # 20% - Very tight trailing, lock in big gains
PROFIT_TIER_4 = 1.30        # 30%+ - Ultra tight, preserve massive wins

# Dynamic trailing stops based on profit level
TRAILING_TIER_1 = 0.03      # 3¢ drawdown at 5-10% profit (loose, let it run)
TRAILING_TIER_2 = 0.02      # 2¢ drawdown at 10-20% profit 
TRAILING_TIER_3 = 0.01      # 1¢ drawdown at 20-30% profit
TRAILING_TIER_4 = 0.005     # 0.5¢ drawdown at 30%+ profit (lock it in tight)

# Safety stops remain strict
STOP_LOSS_PERCENT = 0.10    # 10% loss triggers stop
STOP_LOSS_FLOOR = 0.35      # Absolute floor - never hold below $0.35
MAX_LOSS_PER_TRADE = 0.12   # Maximum 12% loss allowed
TIME_BASED_STOP_LOSS = 2700 # Auto-sell after 45 min if still losing (was 30)
BREAK_EVEN_TIMER = 1800     # Auto-sell at break-even after 30 min (was 15)
REFRESH_RATE = 2

# Risk management
POSITION_ENTRY_TIMES = {}   
MIN_HOLD_TIME = 30          
MAX_POSITION_VALUE = 500    
AUTO_SELL_AT_CLOSE = True

# Trackers
highest_prices = {}
last_prices = {}
last_shares = {}
price_history = {}
known_positions = {}  # Track positions we've seen before
console = Console()

# Setup Kalshi Client
try:
    with open(PRIVATE_KEY_PATH, "r") as f:
        private_key = f.read()
    config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
    config.api_key_id = KEY_ID
    config.private_key_pem = private_key
    client = KalshiClient(config)
except Exception as e:
    console.print(f"[bold red]Setup Error:[/bold red] Check {PRIVATE_KEY_PATH}\nError: {e}")
    exit()

def get_sport_info(ticker):
    """Assigns icons based on ticker strings."""
    t = ticker.upper()
    icons = {"NBA": "🏀", "NHL": "🏒", "SOC": "⚽", "TEN": "🎾", "NFL": "🏈", "MLB": "⚾", "POL": "🏛️"}
    for key, icon in icons.items():
        if key in t: return icon
    return "💰"

def get_sparkline(prices):
    """Generates a tiny bar graph using Unicode block characters with color."""
    if len(prices) < 2: return " "
    chars = " ▁▂▃▄▅▆▇█"
    min_p, max_p = min(prices), max(prices)
    diff = max_p - min_p
    if diff == 0: return "[dim]▄" * len(prices) + "[/dim]"
    
    line = ""
    for i, p in enumerate(prices):
        idx = int(((p - min_p) / diff) * 8)
        idx = min(idx, 7)
        
        # Color gradient: red -> yellow -> green
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
    if not os.path.isfile(LOG_FILE): return 0.0, 0.0
    with open(LOG_FILE, mode='r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                p_val = float(row['PnL%'].replace('%', ''))
                total_pnl += p_val
                total_trades += 1
                if p_val > 0: wins += 1
            except: continue
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    return total_pnl, win_rate

def log_trade(ticker, title, entry, exit_price, pnl_pct, reason):
    """Saves trade data to CSV."""
    log_file = SIMULATED_TRADES_LOG if PAPER_TRADING else LOG_FILE
    file_exists = os.path.isfile(log_file)
    with open(log_file, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Timestamp', 'Ticker', 'Event', 'Entry', 'Exit', 'PnL%', 'Reason', 'Mode'])
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        mode = "SIMULATED" if PAPER_TRADING else "LIVE"
        writer.writerow([timestamp, ticker, title, f"${entry:.2f}", f"${exit_price:.2f}", f"{pnl_pct:.1f}%", reason, mode])

def load_known_positions():
    """Load known positions from log file to avoid re-announcing existing positions."""
    known = {}
    log_file = SIMULATED_TRADES_LOG if PAPER_TRADING else LOG_FILE
    if not os.path.isfile(log_file):
        return known
    
    with open(log_file, mode='r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get('Ticker', '')
            reason = row.get('Reason', '')
            # If we've logged this position before (either as new or sold), track it
            if ticker and 'NEW POSITION' in reason:
                # Extract shares from reason if possible
                try:
                    shares_str = reason.split('(')[1].split(' shares')[0]
                    shares = int(shares_str)
                    position_key = f"{ticker}_{shares}"
                    known[position_key] = True
                except:
                    pass
    return known

def log_new_position(ticker, title, entry, shares):
    """Logs when a new position is detected."""
    log_file = SIMULATED_TRADES_LOG if PAPER_TRADING else LOG_FILE
    file_exists = os.path.isfile(log_file)
    with open(log_file, mode='a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Timestamp', 'Ticker', 'Event', 'Entry', 'Exit', 'PnL%', 'Reason', 'Mode'])
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
        mode = "SIMULATED" if PAPER_TRADING else "LIVE"
        writer.writerow([timestamp, ticker, title, f"${entry:.2f}", "---", "0.0%", f"NEW POSITION ({shares} shares)", mode])
    
    # Console notification with styling
    console.print(f"\n[bold green]🎉 NEW POSITION DETECTED![/bold green]")
    console.print(f"[cyan]📊 {title}[/cyan]")
    console.print(f"[white]💰 Entry: ${entry:.2f} | Shares: {shares}[/white]")
    console.print(f"[dim]🎫 Ticker: {ticker}[/dim]\n")

def execute_order(ticker, shares, reason, action="sell"):
    """
    Executes or simulates order based on PAPER_TRADING mode.
    Returns True if successful, False otherwise.
    """
    if PAPER_TRADING:
        console.print(f"[yellow]📝 SIMULATED {action.upper()}: {ticker} - {shares} shares - {reason}[/yellow]")
        return True
    else:
        try:
            client.create_order(ticker=ticker, action=action, count=shares, type="market", side="yes")
            console.print(f"[green]✅ LIVE {action.upper()}: {ticker} - {shares} shares - {reason}[/green]")
            return True
        except Exception as e:
            console.print(f"[red]❌ Order Error: {e}[/red]")
            return False

def calculate_stop_loss(entry, current_bid):
    """
    Enhanced stop loss calculation:
    - Uses percentage-based stop (15% loss)
    - Has absolute floor ($0.30)
    - Returns the higher of the two for better protection
    """
    percent_stop = entry * (1 - STOP_LOSS_PERCENT)
    return max(percent_stop, STOP_LOSS_FLOOR)

def get_trailing_stop(pnl_percent, peak):
    """
    Dynamic trailing stop based on profit level.
    Higher profits = tighter stops to lock in gains.
    Returns the leash distance and tier info.
    """
    if pnl_percent >= 30:
        return TRAILING_TIER_4, "🔥🔥🔥 HUGE", 4
    elif pnl_percent >= 20:
        return TRAILING_TIER_3, "🔥🔥 BIG", 3
    elif pnl_percent >= 10:
        return TRAILING_TIER_2, "🔥 GOOD", 2
    elif pnl_percent >= 5:
        return TRAILING_TIER_1, "📈 PROFIT", 1
    else:
        return None, None, 0

def should_execute_stop(ticker, current_bid, entry, hold_time):
    """
    SMART stop loss with multiple safety triggers but more patience for winners.
    - Standard percentage stop (10%)
    - Absolute price floor ($0.35)
    - Time-based stops (extended to 45 min for more patience)
    - Break-even protection (30 min instead of 15)
    """
    stop_price = calculate_stop_loss(entry, current_bid)
    pnl_percent = ((current_bid - entry) / entry * 100) if entry > 0 else 0
    
    # Don't stop too early
    if hold_time < MIN_HOLD_TIME:
        return False, "Minimum hold time not met"
    
    # SAFETY 1: Standard stop loss
    if current_bid <= stop_price:
        return True, f"Stop Loss Hit (${current_bid:.2f} <= ${stop_price:.2f})"
    
    # SAFETY 2: Emergency exit for big losses
    if pnl_percent <= -MAX_LOSS_PER_TRADE * 100:
        return True, f"Max Loss Exceeded ({pnl_percent:.1f}%)"
    
    # SAFETY 3: Time-based stop - if losing money for 45+ minutes, exit
    if hold_time >= TIME_BASED_STOP_LOSS and pnl_percent < 0:
        return True, f"Time-Based Stop (Losing for {hold_time/60:.1f} min)"
    
    # SAFETY 4: Break-even protection - after 30 min, exit if stuck near break-even
    if hold_time >= BREAK_EVEN_TIMER and pnl_percent >= -2 and pnl_percent <= 3:
        return True, f"Break-Even Exit (Held {hold_time/60:.1f} min, {pnl_percent:.1f}% PnL)"
    
    return False, None

def generate_dashboard(trades_data):
    """Creates an ULTRA-COOL Rich Table UI with enhanced styling."""
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
    
    mode_indicator = "[yellow bold]📝 PAPER TRADING MODE[/yellow bold]" if PAPER_TRADING else "[cyan bold]⚡ SMART PROFIT MODE[/cyan bold]"
    
    # Enhanced header with more stats
    total_trades = len(trades_data)
    profitable = sum(1 for t in trades_data if t['pnl'] > 0)
    losing = sum(1 for t in trades_data if t['pnl'] < 0)
    
    stats_header = f"{mode_indicator}  |  {perf_emoji} Total PnL: [{p_color}]{all_pnl:+.1f}%[/{p_color}]  |  Win Rate: [cyan]{win_rate:.1f}%[/cyan]  |  Active: [green]{profitable}[/green] / [red]{losing}[/red] / [dim]{total_trades}[/dim]"
    
    table = Table(
        title="🎯 LIVE TRADING DASHBOARD 🎯",
        title_style="bold white on blue",
        border_style="bright_blue",
        header_style="bold cyan",
        show_lines=True,  # Add lines between rows for clarity
        expand=True,
        padding=(0, 1)
    )
    
    # Enhanced columns with better styling
    table.add_column("🎮 Game", style="bold cyan", no_wrap=True, width=30)
    table.add_column("💰 Entry", justify="right", style="white")
    table.add_column("🛑 Stop", justify="right", style="bold yellow")
    table.add_column("🎚️ Tier", justify="center", style="bold magenta")
    table.add_column("⏱️ Time", justify="right", style="dim white")
    table.add_column("📊 Trend", justify="center")
    table.add_column("📈 Chart", justify="center", width=20)
    table.add_column("💵 Now", justify="right", style="bold white")
    table.add_column("🔝 Peak", justify="right", style="dim cyan")
    table.add_column("💎 Profit", justify="right", width=12)
    table.add_column("🎭 Status", justify="center", width=18)

    for data in trades_data:
        pnl_color = "bold green" if data['pnl'] >= 15 else ("green" if data['pnl'] > 0 else "red")
        trend_icon = "[green]↗[/green]" if data['trend'] == "up" else ("[red]↘[/red]" if data['trend'] == "down" else "[grey50]→[/grey50]")
        
        # Format hold time nicely
        hold_min = data['hold_time'] / 60
        if hold_min < 1:
            hold_str = f"{data['hold_time']:.0f}s"
        else:
            hold_str = f"{hold_min:.1f}m"
        
        table.add_row(
            f"{get_sport_info(data['ticker'])} {data['title']}", 
            f"${data['entry']:.2f}", 
            f"${data['stop_loss']:.2f}",
            data['tier_display'],  # Shows what profit tier we're in
            hold_str,
            trend_icon, 
            data['sparkline'],
            f"${data['current']:.2f}", 
            f"${data['peak']:.2f}", 
            f"[{pnl_color}]{data['pnl']:+.1f}%[/{pnl_color}]", 
            data['status']
        )
    
    return Panel(table, title=stats_header, subtitle=f"Update: {datetime.datetime.now().strftime('%I:%M:%S %p')}", border_style="blue")

# --- MAIN LOOP ---
# Load previously known positions from log file
known_positions = load_known_positions()

with Live(generate_dashboard([]), refresh_per_second=1, screen=True) as live:
    while True:
        try:
            response = client.get_positions()
            all_raw = (getattr(response, 'market_positions', []) or []) + (getattr(response, 'event_positions', []) or [])
            formatted_data = []
            current_time = time.time()

            for pos in all_raw:
                shares = abs(int(getattr(pos, 'position', 0)))
                if shares <= 0: continue

                ticker = getattr(pos, 'ticker', getattr(pos, 'event_ticker', 'Unknown'))
                market_res = client.get_market(ticker)
                m = market_res.market
                current_bid = float(m.yes_bid_dollars)
                
                if current_bid < 0.01: continue

                # Track position entry time
                if ticker not in POSITION_ENTRY_TIMES:
                    POSITION_ENTRY_TIMES[ticker] = current_time
                hold_time = current_time - POSITION_ENTRY_TIMES[ticker]

                # Update Price History & Sparkline
                if ticker not in price_history: price_history[ticker] = []
                price_history[ticker].append(current_bid)
                if len(price_history[ticker]) > 15: price_history[ticker].pop(0)
                spark = get_sparkline(price_history[ticker])

                # Peak Tracking & Trend
                if ticker not in last_shares or shares != last_shares[ticker]:
                    highest_prices[ticker] = current_bid
                    last_shares[ticker] = shares

                trend = "steady"
                if ticker in last_prices:
                    if current_bid > last_prices[ticker]: trend = "up"
                    elif current_bid < last_prices[ticker]: trend = "down"
                last_prices[ticker] = current_bid

                # Entry Math
                cost = getattr(pos, 'market_exposure', getattr(pos, 'total_cost', 0))
                entry = (cost / shares) / 100 if cost > 100 else (cost / shares)
                
                # Check if this is a NEW position we haven't seen before
                position_key = f"{ticker}_{shares}"
                if position_key not in known_positions:
                    known_positions[position_key] = True
                    # Log the new position
                    log_new_position(ticker, m.title, entry, shares)
                
                if current_bid > highest_prices.get(ticker, 0):
                    highest_prices[ticker] = current_bid
                peak = highest_prices[ticker]

                # Calculate PnL and Stop Loss
                pnl = ((current_bid - entry) / entry * 100) if entry > 0 else 0.0
                stop_loss_price = calculate_stop_loss(entry, current_bid)
                
                # Position size warning
                position_value = shares * current_bid * 100
                size_warning = "⚠️ " if position_value > MAX_POSITION_VALUE else ""
                
                status = f"{size_warning}📡 [cyan]Tracking[/cyan]"
                tier_display = "[grey50]--[/grey50]"
                
                # SMART PROFIT-TAKING LOGIC (Multi-Tier Trailing)
                leash, tier_name, tier_level = get_trailing_stop(pnl, peak)
                
                if leash is not None:  # We're in profit territory
                    status = f"{size_warning}{tier_name}"
                    tier_display = f"[green]T{tier_level}[/green]"
                    
                    # Check if we've dropped below trailing stop
                    if current_bid <= (peak - leash):
                        if execute_order(ticker, shares, f"{tier_name} Profit Exit (+{pnl:.1f}%)"):
                            log_trade(ticker, m.title, entry, current_bid, pnl, f"Tier {tier_level} Trailing Exit")
                            # Remove from known positions when sold
                            if position_key in known_positions:
                                del known_positions[position_key]
                            if ticker in POSITION_ENTRY_TIMES:
                                del POSITION_ENTRY_TIMES[ticker]
                            if not PAPER_TRADING:
                                continue
                
                # STOP LOSS LOGIC (Safety Net)
                should_stop, stop_reason = should_execute_stop(ticker, current_bid, entry, hold_time)
                if should_stop:
                    status = f"{size_warning}🛑 [bold red]STOP[/bold red]"
                    tier_display = "[red]STOP[/red]"
                    if execute_order(ticker, shares, stop_reason):
                        log_trade(ticker, m.title, entry, current_bid, pnl, stop_reason)
                        # Remove from known positions when sold
                        if position_key in known_positions:
                            del known_positions[position_key]
                        if ticker in POSITION_ENTRY_TIMES:
                            del POSITION_ENTRY_TIMES[ticker]
                        if not PAPER_TRADING:
                            continue

                formatted_data.append({
                    'ticker': ticker, 'title': m.title[:25], 'entry': entry,
                    'stop_loss': stop_loss_price,
                    'tier_display': tier_display,  # Shows profit tier
                    'hold_time': hold_time,
                    'trend': trend, 'sparkline': spark, 'current': current_bid, 
                    'peak': peak, 'pnl': pnl, 'status': status
                })

            formatted_data = sorted(formatted_data, key=lambda x: x['pnl'], reverse=True)
            live.update(generate_dashboard(formatted_data))
            time.sleep(REFRESH_RATE)
            
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Bot stopped by user[/bold yellow]")
            break
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            time.sleep(5)