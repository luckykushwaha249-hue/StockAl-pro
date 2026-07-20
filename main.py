import flet as ft
import sqlite3
import yfinance as yf
import requests
from datetime import datetime
import threading
import time
import shutil

# ==========================================
# COMPATIBILITY SHIMS
# ==========================================
try:
    Icons = ft.Icons
except AttributeError:
    Icons = ft.icons

try:
    Colors = ft.Colors
except AttributeError:
    Colors = ft.colors

# ==========================================
# FALLBACK STOCK LIST 
# ==========================================
FALLBACK_UNIVERSE = [
    ("RELIANCE", "Reliance Industries"), ("TCS", "Tata Consultancy Services"),
    ("HDFCBANK", "HDFC Bank"), ("ICICIBANK", "ICICI Bank"), ("INFY", "Infosys"),
    ("BHARTIARTL", "Bharti Airtel"), ("ITC", "ITC Ltd"), ("SBIN", "State Bank of India"),
    ("LT", "Larsen & Toubro"), ("KOTAKBANK", "Kotak Mahindra Bank"),
    ("AXISBANK", "Axis Bank"), ("HCLTECH", "HCL Technologies"), ("MARUTI", "Maruti Suzuki"),
    ("SUNPHARMA", "Sun Pharma"), ("TITAN", "Titan Company"), ("BAJFINANCE", "Bajaj Finance"),
    ("TATAMOTORS", "Tata Motors"), ("TATASTEEL", "Tata Steel"), ("NTPC", "NTPC Ltd"),
    ("ZOMATO", "Zomato Ltd"), ("JIOFIN", "Jio Financial Services"),
]

# ==========================================
# 1. DATABASE SETUP
# ==========================================
def init_db():
    conn = sqlite3.connect("stockai_pro.db", check_same_thread=False)
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS recent_searches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        query TEXT,
                        search_type TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS favorites (
                        symbol TEXT PRIMARY KEY,
                        company_name TEXT,
                        latest_price REAL,
                        change_pct REAL,
                        last_updated TEXT,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS market_summary (
                        date TEXT PRIMARY KEY,
                        value REAL,
                        sync_time TEXT)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS stock_master (
                        symbol TEXT PRIMARY KEY,
                        company_name TEXT,
                        sector TEXT,
                        price REAL)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS market_movers (
                        date TEXT,
                        type TEXT,
                        rank INTEGER,
                        symbol TEXT,
                        company_name TEXT,
                        price REAL,
                        pct_change REAL)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS news_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT,
                        company_name TEXT,
                        date TEXT,
                        pct_change REAL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT)''')

    # Add columns for favorites if not exist
    for coldef in ("change_pct REAL", "last_updated TEXT"):
        try:
            cursor.execute(f"ALTER TABLE favorites ADD COLUMN {coldef}")
        except Exception:
            pass

    sample_stocks = [(sym, name, "N/A", 0.0) for sym, name in FALLBACK_UNIVERSE]
    cursor.executemany(
        "INSERT OR IGNORE INTO stock_master (symbol, company_name, sector, price) VALUES (?, ?, ?, ?)",
        sample_stocks,
    )
    conn.commit()
    return conn

def get_setting(conn, key, default=None):
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM app_settings WHERE key=?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default

def set_setting(conn, key, value):
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()

# ==========================================
# DATABASE QUERIES & UTILS
# ==========================================
def search_stock_db(conn, query):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, company_name, sector, price FROM stock_master "
        "WHERE symbol LIKE ? OR company_name LIKE ? LIMIT 30",
        (f"%{query}%", f"%{query}%"),
    )
    return cursor.fetchall()

def add_recent_search(conn, query):
    cursor = conn.cursor()
    cursor.execute("INSERT INTO recent_searches (query, search_type) VALUES (?, 'stock')", (query,))
    conn.commit()

def get_recent_searches(conn, limit=5):
    cursor = conn.cursor()
    cursor.execute("SELECT query FROM recent_searches ORDER BY timestamp DESC LIMIT ?", (limit,))
    return [row[0] for row in cursor.fetchall()]

def toggle_favorite(conn, symbol, company_name, price):
    cursor = conn.cursor()
    cursor.execute("SELECT symbol FROM favorites WHERE symbol=?", (symbol,))
    if cursor.fetchone():
        cursor.execute("DELETE FROM favorites WHERE symbol=?", (symbol,))
        conn.commit()
        return False
    cursor.execute(
        "INSERT OR REPLACE INTO favorites (symbol, company_name, latest_price) VALUES (?, ?, ?)",
        (symbol, company_name, price),
    )
    conn.commit()
    return True

def get_favorites(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, company_name, latest_price FROM favorites")
    return cursor.fetchall()

def get_favorites_full(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, company_name, latest_price, change_pct, last_updated FROM favorites ORDER BY symbol")
    return cursor.fetchall()

def get_market_movers(conn, mover_type, date=None):
    cursor = conn.cursor()
    if date is None:
        cursor.execute("SELECT MAX(date) FROM market_movers")
        row = cursor.fetchone()
        date = row[0] if row else None
    if not date:
        return [], None
    cursor.execute(
        "SELECT rank, symbol, company_name, price, pct_change "
        "FROM market_movers WHERE date=? AND type=? ORDER BY rank",
        (date, mover_type),
    )
    return cursor.fetchall(), date

def get_news_items(conn):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM news_items WHERE date < date('now', '-7 days')")
    conn.commit()
    cursor.execute(
        "SELECT symbol, company_name, date, pct_change FROM news_items "
        "ORDER BY date DESC, ABS(pct_change) DESC"
    )
    return cursor.fetchall()

def google_news_url(company_name):
    query = company_name.replace(" ", "+")
    return f"https://news.google.com/search?q={query}%20share%20price&hl=en-IN&gl=IN&ceid=IN:en"

def is_market_open():
    now = datetime.now()
    if now.weekday() >= 5: return False
    open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_time <= now <= close_time

def backup_database():
    try:
        shutil.copy("stockai_pro.db", "stockai_backup.db")
        return "Backup Successful!"
    except Exception as e:
        return f"Backup Failed: {e}"

def clear_cache(conn):
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM recent_searches")
        conn.commit()
        return "Cache Cleared Successfully."
    except Exception as e:
        return f"Clear Failed: {e}"

# ==========================================
# FAST SYNC: NSE API + YFINANCE BACKUP
# ==========================================
def perform_full_market_sync(conn, progress_callback=None):
    try:
        data_date = datetime.now().strftime("%Y-%m-%d")
        sync_timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
        movers = []
        nse_success = False

        # METHOD 1: SUPER-FAST NSE API
        if progress_callback: progress_callback("Fetching Live Nifty 500 data from NSE...")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
            }
            session = requests.Session()
            session.get("https://www.nseindia.com", headers=headers, timeout=5)
            res = session.get("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500", headers=headers, timeout=8)
            
            if res.status_code == 200:
                stocks_data = res.json().get("data", [])
                for item in stocks_data[1:]:
                    sym = item.get("symbol")
                    price = item.get("lastPrice", 0)
                    pChange = item.get("pChange", 0)
                    comp_name = item.get("meta", {}).get("companyName", sym)

                    if sym and str(price).replace('.', '', 1).isdigit():
                        movers.append({
                            "symbol": sym, "company_name": comp_name,
                            "price": float(price), "pct_change": float(pChange)
                        })
                
                if len(movers) > 100:
                    nse_success = True
                    if progress_callback: progress_callback("NSE Data fetched successfully!")
        except Exception:
            pass 

        # METHOD 2: YFINANCE BACKUP (Slower, only if NSE fails)
        if not nse_success:
            if progress_callback: progress_callback("NSE busy. Syncing via Yahoo Finance (Slower)...")
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, company_name FROM stock_master")
            db_stocks = cursor.fetchall()
            if not db_stocks: db_stocks = FALLBACK_UNIVERSE
            
            tickers = [f"{s}.NS" for s, _ in db_stocks[:150]] 
            data = yf.download(tickers=tickers, period="5d", group_by="ticker", threads=True, progress=False)
            
            for s, n in db_stocks[:150]:
                ns = f"{s}.NS"
                try:
                    closes = data[ns]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
                    if len(closes) >= 2:
                        last_c = float(closes.iloc[-1])
                        prev_c = float(closes.iloc[-2])
                        pct = round((last_c - prev_c) / prev_c * 100, 2)
                        movers.append({"symbol": s, "company_name": n, "price": round(last_c, 2), "pct_change": pct})
                except: continue

        if not movers:
            return "Sync Failed: Internet issue or APIs blocked.", False

        # UPDATE DATABASE WITH MOVERS
        cursor = conn.cursor()
        gainers = sorted(movers, key=lambda x: x["pct_change"], reverse=True)[:10]
        losers = sorted(movers, key=lambda x: x["pct_change"])[:10]

        cursor.execute("DELETE FROM market_movers WHERE date=?", (data_date,))
        for rank, m in enumerate(gainers, 1):
            cursor.execute("INSERT INTO market_movers (date, type, rank, symbol, company_name, price, pct_change) VALUES (?,?,?,?,?,?,?)",
                           (data_date, "gainer", rank, m["symbol"], m["company_name"], m["price"], m["pct_change"]))
        for rank, m in enumerate(losers, 1):
            cursor.execute("INSERT INTO market_movers (date, type, rank, symbol, company_name, price, pct_change) VALUES (?,?,?,?,?,?,?)",
                           (data_date, "loser", rank, m["symbol"], m["company_name"], m["price"], m["pct_change"]))

        # Update Master stock DB
        for m in movers:
            cursor.execute("INSERT OR REPLACE INTO stock_master (symbol, company_name, sector, price) VALUES (?, ?, 'N/A', ?)",
                           (m["symbol"], m["company_name"], m["price"]))

        # WATCHLIST SPECIFIC UPDATE (If watchlist stock not in Nifty500)
        cursor.execute("SELECT symbol FROM favorites")
        fav_symbols = [row[0] for row in cursor.fetchall()]
        movers_dict = {m["symbol"]: m for m in movers}

        missing_favs = [s for s in fav_symbols if s not in movers_dict]
        if missing_favs:
            if progress_callback: progress_callback("Updating personal watchlist stocks...")
            try:
                tickers = [f"{s}.NS" for s in missing_favs]
                fav_data = yf.download(tickers=tickers, period="5d", group_by="ticker", threads=True, progress=False)
                for s in missing_favs:
                    ns = f"{s}.NS"
                    try:
                        closes = fav_data[ns]["Close"].dropna() if len(missing_favs) > 1 else fav_data["Close"].dropna()
                        if len(closes) >= 2:
                            last_c = float(closes.iloc[-1])
                            prev_c = float(closes.iloc[-2])
                            pct = round((last_c - prev_c) / prev_c * 100, 2)
                            movers_dict[s] = {"price": round(last_c, 2), "pct_change": pct}
                    except: pass
            except: pass

        # Save watchlist updates
        for symbol in fav_symbols:
            if symbol in movers_dict:
                m = movers_dict[symbol]
                cursor.execute(
                    "UPDATE favorites SET latest_price=?, change_pct=?, last_updated=? WHERE symbol=?",
                    (m["price"], m["pct_change"], sync_timestamp, symbol),
                )

        # Update News Items
        seen = set()
        cursor.execute("DELETE FROM news_items WHERE date=?", (data_date,))
        for m in (gainers + losers):
            if m["symbol"] not in seen:
                seen.add(m["symbol"])
                cursor.execute("INSERT INTO news_items (symbol, company_name, date, pct_change) VALUES (?,?,?,?)",
                               (m["symbol"], m["company_name"], data_date, m["pct_change"]))

        cursor.execute("INSERT OR REPLACE INTO market_summary (date, value, sync_time) VALUES (?, ?, ?)",
                       (data_date, gainers[0]["price"] if gainers else 0, sync_timestamp))
        conn.commit()

        status = "Live" if is_market_open() else "Closed"
        return f"Synced ({status}) - data as of {sync_timestamp}", True
    except Exception as e:
        return f"Sync Error: {str(e)}", False


# ==========================================
# 2. MAIN APP & UI
# ==========================================
def main(page: ft.Page):
    page.title = "StockAI Pro"
    page.padding = 0
    db_conn = init_db()

    # Apply saved theme
    stored_theme = get_setting(db_conn, "theme_mode", "system")
    if stored_theme == "light": page.theme_mode = ft.ThemeMode.LIGHT
    elif stored_theme == "dark": page.theme_mode = ft.ThemeMode.DARK
    else: page.theme_mode = ft.ThemeMode.SYSTEM
    
    page.theme = ft.Theme(color_scheme_seed="blue", use_material3=True)
    main_content = ft.Container(expand=True)

    # ---------- SPLASH & ERROR ----------
    splash_screen = ft.Container(
        expand=True, alignment=ft.alignment.center,
        content=ft.Column(
            [
                ft.Icon(Icons.SHOW_CHART, size=90, color=Colors.BLUE_700),
                ft.Text("StockAI Pro", size=36, weight=ft.FontWeight.W_900),
                ft.Text("Personal AI Stock Research Terminal", size=14, color=Colors.GREY_500, italic=True),
                ft.Container(height=40),
                ft.ProgressBar(width=150, color=Colors.BLUE_700),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER,
        ),
    )

    def show_error_screen(message):
        main_content.content = ft.Container(
            expand=True, alignment=ft.alignment.center, padding=20,
            content=ft.Column([
                ft.Icon(Icons.ERROR_OUTLINE, size=60, color=Colors.RED),
                ft.Text("Something went wrong", size=20, weight=ft.FontWeight.BOLD),
                ft.Text(str(message), size=12, color=Colors.GREY_600, selectable=True),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        )
        page.update()

    # ---------- STOCK DETAILS PAGE (OLD FEATURE RESTORED) ----------
    def show_stock_details(symbol, company_name, sector, price):
        is_fav = any(f[0] == symbol for f in get_favorites(db_conn))
        fav_icon = ft.Icon(Icons.STAR if is_fav else Icons.STAR_BORDER)

        def go_back(e):
            main_content.content = home_screen
            page.update()

        def on_fav_click(e):
            added = toggle_favorite(db_conn, symbol, company_name, price)
            fav_icon.name = Icons.STAR if added else Icons.STAR_BORDER
            refresh_watchlist_list()
            page.update()

        details_page = ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.IconButton(Icons.ARROW_BACK, on_click=go_back),
                    ft.Text(f"{symbol} Details", size=24, weight=ft.FontWeight.BOLD),
                    ft.IconButton(content=fav_icon, on_click=on_fav_click, tooltip="Add/Remove Watchlist"),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(),
                ft.Card(
                    content=ft.Container(
                        padding=20,
                        content=ft.Column([
                            ft.Text(f"Sector: {sector}", color=Colors.GREY_600),
                            ft.Text(f"Price: Rs.{price:,.2f}" if price else "Price: not synced yet",
                                    size=32, weight=ft.FontWeight.W_800),
                            ft.Text(company_name, color=Colors.GREY_700),
                        ]),
                    )
                ),
                ft.ElevatedButton(
                    "Read News on Google", icon=Icons.OPEN_IN_NEW,
                    on_click=lambda e: page.launch_url(google_news_url(company_name)),
                ),
            ]),
        )
        main_content.content = details_page
        page.update()

    # ---------- HOME SCREEN ----------
    search_input = ft.TextField(
        hint_text="Search Stock (e.g. RELIANCE, TCS)",
        prefix_icon=Icons.SEARCH, border_radius=30, filled=True,
        border_color=Colors.TRANSPARENT, height=55, text_size=16,
    )
    result_column = ft.Column(spacing=8)
    recent_list = ft.Column()

    def refresh_recent_list():
        recent_list.controls.clear()
        recents = get_recent_searches(db_conn)
        if not recents:
            recent_list.controls.append(
                ft.Container(content=ft.Row([ft.Icon(Icons.HISTORY, color=Colors.GREY_400), ft.Text("No recent searches", color=Colors.GREY_500)]), padding=10)
            )
        else:
            for q in recents:
                recent_list.controls.append(ft.ListTile(leading=ft.Icon(Icons.HISTORY), title=ft.Text(q), on_click=lambda e, q=q: run_search(q)))

    def run_search(query):
        results = search_stock_db(db_conn, query)
        result_column.controls.clear()
        if results:
            add_recent_search(db_conn, query)
            refresh_recent_list()
            for symbol, company_name, sector, price in results:
                result_column.controls.append(
                    ft.ListTile(
                        leading=ft.Icon(Icons.SHOW_CHART),
                        title=ft.Text(f"{symbol} - {company_name}"),
                        subtitle=ft.Text(f"Rs.{price:,.2f}" if price else "Not synced yet"),
                        on_click=lambda e, s=symbol, c=company_name, sec=sector, p=price: show_stock_details(s, c, sec, p),
                    )
                )
        else:
            result_column.controls.append(ft.Text(f"No results found for '{query}'", color=Colors.GREY_500))
        page.update()

    def handle_search(e):
        query = (search_input.value or "").strip()
        if query: run_search(query)

    search_input.on_submit = handle_search

    cursor = db_conn.cursor()
    cursor.execute("SELECT sync_time FROM market_summary ORDER BY date DESC LIMIT 1")
    row = cursor.fetchone()
    sync_status_text = ft.Text(f"Last updated: {row[0]}" if row else "Not synced yet. Tap 'Update Market Data'.", size=12, color=Colors.GREY)

    def on_update_market_data(e):
        update_button.disabled = True
        update_button.text = "Syncing..."
        page.update()

        def progress_callback(msg):
            sync_status_text.value = msg
            page.update()

        def do_sync():
            msg, success = perform_full_market_sync(db_conn, progress_callback)
            sync_status_text.value = msg
            sync_status_text.color = Colors.GREEN if success else Colors.RED
            update_button.disabled = False
            update_button.text = "Update Market Data"
            refresh_watchlist_list()
            refresh_news_screen()
            refresh_analytics_screen()
            page.update()

        threading.Thread(target=do_sync, daemon=True).start()

    update_button = ft.ElevatedButton("Update Market Data", icon=Icons.REFRESH, on_click=on_update_market_data, style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=15), padding=15))

    home_screen = ft.Container(
        expand=True, padding=20,
        content=ft.Column([
            ft.Container(height=20),
            ft.Text("StockAI Pro", size=28, weight=ft.FontWeight.BOLD),
            ft.Text("Market data ready for analysis", size=14, color=Colors.BLUE_600),
            ft.Container(height=15),
            update_button,
            sync_status_text,
            ft.Container(height=15),
            search_input,
            result_column,
            ft.Container(height=30),
            ft.Text("Recent Searches", size=18, weight=ft.FontWeight.W_700),
            recent_list,
        ], scroll=ft.ScrollMode.AUTO)
    )

    # ---------- NEWS SCREEN (OLD FEATURE RESTORED) ----------
    news_list = ft.Column(spacing=10)

    def refresh_news_screen():
        news_list.controls.clear()
        items = get_news_items(db_conn)
        if not items:
            news_list.controls.append(ft.Text("No news yet. Tap 'Update Market Data' on Home.", color=Colors.GREY_500))
        else:
            for symbol, company_name, date, pct_change in items:
                up = pct_change >= 0
                news_list.controls.append(
                    ft.Card(
                        content=ft.Container(
                            padding=15,
                            content=ft.Column([
                                ft.Row([
                                    ft.Icon(Icons.TRENDING_UP if up else Icons.TRENDING_DOWN, color=Colors.GREEN if up else Colors.RED),
                                    ft.Text(f"{symbol} {'gained' if up else 'fell'} {abs(pct_change):.2f}% today", weight=ft.FontWeight.BOLD, size=15, expand=True),
                                ]),
                                ft.Text(f"{company_name} - {date}", size=12, color=Colors.GREY_500),
                                ft.TextButton("Read on Google News", icon=Icons.OPEN_IN_NEW, on_click=lambda e, c=company_name: page.launch_url(google_news_url(c))),
                            ], spacing=4),
                        )
                    )
                )

    news_screen = ft.Container(
        padding=20,
        content=ft.Column([
            ft.Text("Market Movers - News", size=24, weight=ft.FontWeight.BOLD),
            ft.Text("Auto-clears after 7 days", size=12, color=Colors.GREY_500),
            ft.Container(height=15),
            news_list,
        ], scroll=ft.ScrollMode.AUTO)
    )

    # ---------- WATCHLIST & SCREENER SCREEN (NEW FEATURE ADDED) ----------
    watchlist_list = ft.Column(spacing=8)
    screener_results = ft.Column(spacing=2)

    def add_from_screener(symbol, name, price):
        cursor = db_conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO favorites (symbol, company_name, latest_price) VALUES (?, ?, ?)", (symbol, name, price))
        db_conn.commit()
        screener_search.value = ""
        screener_results.controls.clear()
        refresh_watchlist_list()
        page.update()

    def on_screener_change(e):
        query = screener_search.value.strip()
        screener_results.controls.clear()
        if len(query) >= 3:
            results = search_stock_db(db_conn, query)
            if results:
                for sym, name, sec, price in results:
                    screener_results.controls.append(
                        ft.ListTile(
                            leading=ft.Icon(Icons.ADD_CIRCLE_OUTLINE, color=Colors.BLUE),
                            title=ft.Text(f"{sym} - {name}", size=14),
                            on_click=lambda e, s=sym, n=name, p=price: add_from_screener(s, n, p)
                        )
                    )
            else:
                screener_results.controls.append(ft.Text("No valid stock found", color=Colors.RED_400, size=12))
        page.update()

    screener_search = ft.TextField(
        hint_text="Search stock to add (min 3 chars)...",
        prefix_icon=Icons.SEARCH, border_radius=25, height=48, text_size=14,
        on_change=on_screener_change
    )

    def refresh_watchlist_list():
        watchlist_list.controls.clear()
        favs = get_favorites_full(db_conn)
        if not favs:
            watchlist_list.controls.append(ft.Text("Watchlist is empty. Search above to add.", color=Colors.GREY_500))
        else:
            for symbol, company_name, price, change_pct, last_updated in favs:
                up = (change_pct or 0) >= 0
                pct_text = f"{change_pct:+.2f}%" if change_pct is not None else "N/A"
                price_text = f"Rs.{price:,.2f}" if price else "N/A"

                watchlist_list.controls.append(
                    ft.Card(
                        content=ft.ListTile(
                            leading=ft.Icon(Icons.SHOW_CHART),
                            title=ft.Text(symbol, weight=ft.FontWeight.BOLD),
                            subtitle=ft.Text(f"{price_text} | {company_name}", size=12, color=Colors.GREY_400),
                            trailing=ft.Row(
                                [
                                    ft.Text(pct_text, color=Colors.GREEN if up else Colors.RED, weight=ft.FontWeight.BOLD),
                                    ft.IconButton(Icons.DELETE_OUTLINE, icon_color=Colors.RED_300, 
                                                  on_click=lambda e, s=symbol: [cursor.execute("DELETE FROM favorites WHERE symbol=?", (s,)), db_conn.commit(), refresh_watchlist_list(), page.update()]),
                                ], tight=True
                            ),
                        )
                    )
                )

    watchlist_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("My Watchlist", size=24, weight=ft.FontWeight.BOLD),
                ft.Text("Smart Search enabled (prevents fake entries)", size=12, color=Colors.GREY_500),
                screener_search,
                screener_results,
                ft.Divider(height=20),
                watchlist_list,
            ], scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- ANALYTICS SCREEN ----------
    analytics_content = ft.Column(spacing=15)
    analytics_date_text = ft.Text("No data", size=13, color=Colors.GREY_500)

    def refresh_analytics_screen():
        gainers, date1 = get_market_movers(db_conn, "gainer")
        losers, date2 = get_market_movers(db_conn, "loser")
        
        analytics_date_text.value = f"Data Date: {date1 or 'None'}"
        analytics_content.controls.clear()

        for title, data, color, icon in [("Top 10 Gainers", gainers, Colors.GREEN, Icons.TRENDING_UP), 
                                         ("Top 10 Losers", losers, Colors.RED, Icons.TRENDING_DOWN)]:
            rows = ft.Column(spacing=2)
            for rank, symbol, comp, price, pct in data:
                rows.controls.append(
                    ft.ListTile(
                        leading=ft.Text(f"#{rank}", color=color, weight=ft.FontWeight.BOLD),
                        title=ft.Text(symbol, weight=ft.FontWeight.BOLD),
                        subtitle=ft.Text(f"Rs.{price:,.2f}"),
                        trailing=ft.Text(f"{pct:+.2f}%", color=color, weight=ft.FontWeight.BOLD)
                    )
                )
            analytics_content.controls.append(
                ft.Card(content=ft.Container(padding=10, content=ft.Column([
                    ft.Row([ft.Icon(icon, color=color), ft.Text(title, size=18, weight=ft.FontWeight.BOLD)]), rows
                ])))
            )

    analytics_screen = ft.Container(
        padding=20,
        content=ft.Column([
            ft.Text("Market Analytics", size=24, weight=ft.FontWeight.BOLD),
            analytics_date_text,
            analytics_content,
        ], scroll=ft.ScrollMode.AUTO)
    )

    # ---------- SETTINGS SCREEN (OLD FEATURE RESTORED) ----------
    status_text = ft.Text("")

    def on_backup(e):
        status_text.value = backup_database()
        page.update()

    def on_clear(e):
        status_text.value = clear_cache(db_conn)
        refresh_recent_list()
        page.update()

    def on_theme_change(e):
        val = theme_dropdown.value
        set_setting(db_conn, "theme_mode", val)
        if val == "light": page.theme_mode = ft.ThemeMode.LIGHT
        elif val == "dark": page.theme_mode = ft.ThemeMode.DARK
        else: page.theme_mode = ft.ThemeMode.SYSTEM
        page.update()

    theme_dropdown = ft.Dropdown(
        label="App Theme", value=stored_theme,
        options=[
            ft.dropdown.Option("system", "System Default"),
            ft.dropdown.Option("light", "Light"),
            ft.dropdown.Option("dark", "Dark"),
        ],
        on_change=on_theme_change,
    )

    settings_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Settings", size=28, weight=ft.FontWeight.BOLD),
                ft.Container(height=15),
                ft.Text("Appearance", size=18, weight=ft.FontWeight.W_600),
                theme_dropdown,
                ft.Container(height=20),
                ft.Text("Database Management", size=18, weight=ft.FontWeight.W_600),
                ft.Card(content=ft.Column([
                    ft.ListTile(leading=ft.Icon(Icons.BACKUP), title=ft.Text("Backup Database"), on_click=on_backup),
                    ft.ListTile(leading=ft.Icon(Icons.DELETE_FOREVER), title=ft.Text("Clear Search History"), on_click=on_clear, icon_color=Colors.RED),
                ])),
                ft.Container(height=10),
                status_text,
                ft.Container(height=20),
                ft.Text("App Info", size=18, weight=ft.FontWeight.W_600),
                ft.Card(content=ft.Container(padding=15, content=ft.Column([
                    ft.Text("Version: 2.0 (Fast NSE Sync + Screener)"),
                    ft.Text("Database: Local SQLite"),
                    ft.Text("Data source: NSE API + Yahoo Finance Fallback"),
                ]))),
            ], scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- BOTTOM NAV ----------
    screens = [home_screen, news_screen, watchlist_screen, analytics_screen, settings_screen]

    def change_tab(e):
        idx = e.control.selected_index
        if idx == 1: refresh_news_screen()
        elif idx == 2: refresh_watchlist_list()
        elif idx == 3: refresh_analytics_screen()
        main_content.content = screens[idx]
        page.update()

    bottom_nav = ft.NavigationBar(
        selected_index=0, on_change=change_tab,
        destinations=[
            ft.NavigationBarDestination(icon=Icons.HOME_OUTLINED, selected_icon=Icons.HOME, label="Home"),
            ft.NavigationBarDestination(icon=Icons.ARTICLE_OUTLINED, selected_icon=Icons.ARTICLE, label="News"),
            ft.NavigationBarDestination(icon=Icons.STAR_BORDER, selected_icon=Icons.STAR, label="Watchlist"),
            ft.NavigationBarDestination(icon=Icons.ANALYTICS_OUTLINED, selected_icon=Icons.ANALYTICS, label="Analytics"),
            ft.NavigationBarDestination(icon=Icons.SETTINGS_OUTLINED, selected_icon=Icons.SETTINGS, label="Settings"),
        ],
    )

    # ---------- BACKGROUND SYNC (OLD FEATURE RESTORED) ----------
    def background_auto_sync():
        try:
            last_sync = get_setting(db_conn, "last_auto_sync_date")
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            # Auto sync logic after 16:00 (Market close)
            if now.hour >= 16 and last_sync != today_str:
                def progress_callback(msg):
                    sync_status_text.value = msg
                    page.update()

                msg, success = perform_full_market_sync(db_conn, progress_callback)
                if success:
                    set_setting(db_conn, "last_auto_sync_date", today_str)
                sync_status_text.value = msg
                sync_status_text.color = Colors.GREEN if success else Colors.RED
                refresh_watchlist_list()
                refresh_news_screen()
                refresh_analytics_screen()
                page.update()
        except Exception:
            pass

    # ---------- PASSWORD LOCK SCREEN (OLD FEATURE RESTORED) ----------
    APP_PASSWORD = "8707352902"
    password_input = ft.TextField(
        hint_text="Enter Password", password=True, can_reveal_password=True,
        border_radius=30, filled=True, border_color=Colors.TRANSPARENT,
        height=55, text_align=ft.TextAlign.CENTER, width=260,
    )
    password_error = ft.Text("", color=Colors.RED, size=13)

    def load_home():
        try:
            time.sleep(2)
            refresh_recent_list()
            refresh_watchlist_list()
            refresh_news_screen()
            refresh_analytics_screen()
            main_content.content = home_screen
            page.navigation_bar = bottom_nav
            page.update()
            threading.Thread(target=background_auto_sync, daemon=True).start()
        except Exception as ex:
            show_error_screen(f"Failed to load home screen:\n{ex}")

    def check_password(e):
        if (password_input.value or "").strip() == APP_PASSWORD:
            password_error.value = ""
            main_content.content = splash_screen
            page.update()
            threading.Thread(target=load_home, daemon=True).start()
        else:
            password_error.value = "Wrong password. Try again."
            password_input.value = ""
            page.update()

    password_input.on_submit = check_password

    password_screen = ft.Container(
        expand=True, alignment=ft.alignment.center,
        content=ft.Column(
            [
                ft.Icon(Icons.LOCK, size=70, color=Colors.BLUE_700),
                ft.Text("StockAI Pro", size=28, weight=ft.FontWeight.BOLD),
                ft.Text("Enter password to continue", size=14, color=Colors.GREY_500),
                ft.Container(height=20),
                password_input,
                ft.Container(height=10),
                ft.ElevatedButton("OK", on_click=check_password, width=260),
                ft.Container(height=8),
                password_error,
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER,
        ),
    )

    # ---------- INITIAL RENDER ----------
    main_content.content = password_screen
    page.add(main_content)

if __name__ == "__main__":
    ft.app(target=main)
