import flet as ft
import sqlite3
import yfinance as yf
from datetime import datetime
import threading
import time
import shutil

# ==========================================
# COMPATIBILITY SHIMS (works on old & new Flet)
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

    sample_stocks = [
        ("RELIANCE", "Reliance Industries", "Energy", 2450.00),
        ("TCS", "Tata Consultancy Services", "Technology", 3890.50),
        ("INFY", "Infosys", "Technology", 1560.25),
        ("HDFCBANK", "HDFC Bank", "Banking", 1650.75),
        ("ICICIBANK", "ICICI Bank", "Banking", 1180.30),
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO stock_master (symbol, company_name, sector, price) VALUES (?, ?, ?, ?)",
        sample_stocks,
    )
    conn.commit()
    return conn


def search_stock_db(conn, query):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM stock_master WHERE symbol LIKE ? OR company_name LIKE ?",
        (f"%{query}%", f"%{query}%"),
    )
    return cursor.fetchall()


def add_recent_search(conn, query, search_type="stock"):
    cursor = conn.cursor()
    cursor.execute("INSERT INTO recent_searches (query, search_type) VALUES (?, ?)", (query, search_type))
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


def sync_market_data(conn):
    """Fetch Nifty 50 close price after 4 PM and store it."""
    now = datetime.now()
    if now.hour < 16:
        return "Waiting for 4 PM to sync...", False
    try:
        data = yf.Ticker("^NSEI").history(period="1d")
        close_price = round(data["Close"].iloc[-1], 2)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO market_summary (date, value, sync_time) VALUES (?, ?, ?)",
            (now.strftime("%Y-%m-%d"), close_price, now.strftime("%H:%M")),
        )
        conn.commit()
        return f"Nifty 50 Synced: ₹{close_price} at {now.strftime('%H:%M')}", True
    except Exception:
        return "Sync Failed (No Internet / Data Unavailable)", False


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


def run_growth_scanner():
    return [
        {"name": "Reliance", "growth": "35%"},
        {"name": "TCS", "growth": "28%"},
        {"name": "Infosys", "growth": "22%"},
    ]


# ==========================================
# 2. MAIN APPLICATION
# ==========================================
def main(page: ft.Page):
    page.title = "StockAI Pro"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme_seed="blue", use_material3=True)
    page.padding = 0

    try:
        page.window.width = 400
        page.window.height = 800
    except AttributeError:
        page.window_width = 400
        page.window_height = 800

    db_conn = init_db()

    main_content = ft.Container(expand=True)

    # ---------- SPLASH SCREEN ----------
    splash_screen = ft.Container(
        expand=True,
        alignment=ft.alignment.center,
        content=ft.Column(
            [
                ft.Icon(Icons.SHOW_CHART, size=90, color=Colors.BLUE_700),
                ft.Text("StockAI Pro", size=36, weight=ft.FontWeight.W_900),
                ft.Text("Personal AI Stock Research Terminal", size=14, color=Colors.GREY_500, italic=True),
                ft.Container(height=40),
                ft.ProgressBar(width=150, color=Colors.BLUE_700),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        ),
    )

    # ---------- STOCK DETAILS PAGE ----------
    def show_stock_details(symbol, company_name, sector, price):
        is_fav = any(f[0] == symbol for f in get_favorites(db_conn))
        fav_icon = ft.Icon(Icons.STAR if is_fav else Icons.STAR_BORDER)

        def go_back(e):
            main_content.content = home_screen
            page.update()

        def on_fav_click(e):
            added = toggle_favorite(db_conn, symbol, company_name, price)
            fav_icon.name = Icons.STAR if added else Icons.STAR_BORDER
            refresh_favorites_list()
            page.update()

        details_page = ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.IconButton(Icons.ARROW_BACK, on_click=go_back),
                    ft.Text(f"{symbol} Details", size=24, weight=ft.FontWeight.BOLD),
                    ft.IconButton(content=fav_icon, on_click=on_fav_click),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(),
                ft.Card(
                    content=ft.Container(
                        padding=20,
                        content=ft.Column([
                            ft.Text(f"Sector: {sector}", color=Colors.GREY_600),
                            ft.Text(f"Price: ₹{price:,.2f}", size=32, weight=ft.FontWeight.W_800),
                            ft.Text(company_name, color=Colors.GREY_700),
                        ]),
                    )
                ),
                ft.Tabs(
                    selected_index=0,
                    tabs=[
                        ft.Tab(text="Overview", content=ft.Container(padding=15, content=ft.Text("Company overview details here..."))),
                        ft.Tab(text="AI Analysis", content=ft.Container(padding=15, content=ft.Text("AI insights based on history..."))),
                        ft.Tab(text="News", content=ft.Container(padding=15, content=ft.Text("Latest company news..."))),
                    ],
                ),
            ]),
        )
        main_content.content = details_page
        page.update()

    # ---------- HOME SCREEN ----------
    search_input = ft.TextField(
        hint_text="Search Stock (e.g. RELIANCE, TCS)",
        prefix_icon=Icons.SEARCH,
        border_radius=30,
        filled=True,
        border_color=Colors.TRANSPARENT,
        height=55,
        text_size=16,
    )

    result_column = ft.Column(spacing=8)
    recent_list = ft.Column()
    favorites_list = ft.Column()

    def refresh_recent_list():
        recent_list.controls.clear()
        recents = get_recent_searches(db_conn)
        if not recents:
            recent_list.controls.append(
                ft.Container(
                    content=ft.Row([
                        ft.Icon(Icons.HISTORY, color=Colors.GREY_400),
                        ft.Text("No recent searches", color=Colors.GREY_500),
                    ]),
                    padding=10,
                )
            )
        else:
            for q in recents:
                recent_list.controls.append(
                    ft.ListTile(
                        leading=ft.Icon(Icons.HISTORY),
                        title=ft.Text(q),
                        on_click=lambda e, q=q: run_search(q),
                    )
                )

    def refresh_favorites_list():
        favorites_list.controls.clear()
        favs = get_favorites(db_conn)
        if not favs:
            favorites_list.controls.append(
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Icon(Icons.STAR_BORDER, size=60, color=Colors.GREY_300),
                            ft.Text("No Favorite Stocks Yet", color=Colors.GREY_500, weight=ft.FontWeight.W_600),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    alignment=ft.alignment.center,
                    padding=30,
                )
            )
        else:
            for symbol, company_name, price in favs:
                favorites_list.controls.append(
                    ft.ListTile(
                        leading=ft.Icon(Icons.STAR, color=Colors.AMBER),
                        title=ft.Text(symbol, weight=ft.FontWeight.BOLD),
                        subtitle=ft.Text(f"₹{price:,.2f}"),
                        on_click=lambda e, s=symbol, c=company_name, p=price: show_stock_details(s, c, "N/A", p),
                    )
                )

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
                        subtitle=ft.Text(f"{sector} | ₹{price:,.2f}"),
                        on_click=lambda e, s=symbol, c=company_name, sec=sector, p=price: show_stock_details(s, c, sec, p),
                    )
                )
        else:
            result_column.controls.append(ft.Text(f"No results found for '{query}'", color=Colors.GREY_500))
        page.update()

    def handle_search(e):
        query = (search_input.value or "").strip()
        if query:
            run_search(query)

    search_input.on_submit = handle_search

    btn_search_stock = ft.ElevatedButton(
        "Search by Stock",
        icon=Icons.BAR_CHART,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=15), padding=15),
        on_click=handle_search,
        expand=True,
    )

    btn_search_date = ft.ElevatedButton(
        "Search by Date",
        icon=Icons.CALENDAR_MONTH,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=15), padding=15),
        expand=True,
    )

    home_screen = ft.Container(
        expand=True,
        padding=20,
        content=ft.Column(
            [
                ft.Container(height=30),
                ft.Text("StockAI Pro", size=28, weight=ft.FontWeight.BOLD),
                ft.Text("Market data ready for analysis", size=14, color=Colors.BLUE_600),
                ft.Container(height=20),
                search_input,
                ft.Container(height=10),
                ft.Row([btn_search_stock, btn_search_date], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, spacing=15),
                result_column,
                ft.Container(height=35),
                ft.Text("Recent Searches", size=18, weight=ft.FontWeight.W_700),
                recent_list,
                ft.Container(height=25),
                ft.Text("Favorite Stocks", size=18, weight=ft.FontWeight.W_700),
                favorites_list,
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- ANALYTICS SCREEN ----------
    market_status = ft.Text("Checking sync status...", color=Colors.GREY)

    def trigger_sync(e):
        msg, success = sync_market_data(db_conn)
        market_status.value = msg
        market_status.color = Colors.GREEN if success else Colors.RED
        page.update()

    analytics_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Analytics Dashboard", size=24, weight=ft.FontWeight.BOLD),
                ft.Container(height=20),
                ft.Card(
                    content=ft.Container(
                        padding=20,
                        content=ft.Column([
                            ft.Text("Market Summary", size=18, weight=ft.FontWeight.BOLD),
                            market_status,
                            ft.ElevatedButton("Sync Data Now", on_click=trigger_sync),
                        ]),
                    )
                ),
                ft.Container(height=20),
                ft.Text("Top 10 Gainers", size=18, weight=ft.FontWeight.BOLD),
                ft.Container(height=100, bgcolor=Colors.BLUE_GREY_50, content=ft.Text("RELIANCE, TCS, INFY...")),
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- SCANNER SCREEN ----------
    scanner_cards = ft.Column(spacing=10)
    for stock in run_growth_scanner():
        scanner_cards.controls.append(
            ft.Card(
                content=ft.ListTile(
                    leading=ft.Icon(Icons.SHOW_CHART),
                    title=ft.Text(stock["name"], weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text(f"Growth: {stock['growth']}"),
                    trailing=ft.Icon(Icons.ARROW_FORWARD_IOS, size=15),
                )
            )
        )

    scanner_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Advanced Scanners", size=24, weight=ft.FontWeight.BOLD),
                ft.Text("Analyze historical data instantly", color=Colors.GREY),
                ft.Container(height=20),
                ft.Text("4 Quarter Growth", size=18, weight=ft.FontWeight.W_600),
                scanner_cards,
                ft.Container(height=20),
                ft.Text("High Volume Activity", size=18, weight=ft.FontWeight.W_600),
                ft.Card(content=ft.Container(padding=20, content=ft.Text("No high volume spikes today."))),
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- HISTORY SCREEN ----------
    history_list = ft.Column()

    def refresh_history_screen():
        history_list.controls.clear()
        recents = get_recent_searches(db_conn, limit=50)
        if not recents:
            history_list.controls.append(ft.Text("No search history yet.", color=Colors.GREY_500))
        else:
            for q in recents:
                history_list.controls.append(ft.ListTile(leading=ft.Icon(Icons.HISTORY), title=ft.Text(q)))

    history_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Search History", size=24, weight=ft.FontWeight.BOLD),
                ft.Container(height=10),
                history_list,
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- SETTINGS SCREEN ----------
    status_text = ft.Text("")

    def on_backup(e):
        status_text.value = backup_database()
        page.update()

    def on_clear(e):
        status_text.value = clear_cache(db_conn)
        refresh_recent_list()
        refresh_history_screen()
        page.update()

    settings_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Settings", size=28, weight=ft.FontWeight.BOLD),
                ft.Container(height=20),
                ft.Text("Database Management", size=18, weight=ft.FontWeight.W_600),
                ft.Card(content=ft.Column([
                    ft.ListTile(leading=ft.Icon(Icons.BACKUP), title=ft.Text("Backup Database"), on_click=on_backup),
                    ft.ListTile(leading=ft.Icon(Icons.DELETE_FOREVER), title=ft.Text("Clear All Data"), on_click=on_clear, icon_color=Colors.RED),
                ])),
                ft.Container(height=10),
                status_text,
                ft.Container(height=20),
                ft.Text("App Info", size=18, weight=ft.FontWeight.W_600),
                ft.Card(content=ft.Container(padding=15, content=ft.Column([
                    ft.Text("Version: 1.0.0 (StockAI Pro)"),
                    ft.Text("Database: Local SQLite"),
                    ft.Text("Last Sync: Check Analytics tab"),
                ]))),
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- BOTTOM NAVIGATION ----------
    screens = [home_screen, analytics_screen, scanner_screen, history_screen, settings_screen]

    def change_tab(e):
        idx = e.control.selected_index
        if idx == 3:
            refresh_history_screen()
        main_content.content = screens[idx]
        page.update()

    bottom_nav = ft.NavigationBar(
        selected_index=0,
        on_change=change_tab,
        destinations=[
            ft.NavigationDestination(icon=Icons.HOME_OUTLINED, selected_icon=Icons.HOME, label="Home"),
            ft.NavigationDestination(icon=Icons.ANALYTICS_OUTLINED, selected_icon=Icons.ANALYTICS, label="Analytics"),
            ft.NavigationDestination(icon=Icons.DOCUMENT_SCANNER_OUTLINED, selected_icon=Icons.DOCUMENT_SCANNER, label="Scanner"),
            ft.NavigationDestination(icon=Icons.HISTORY_OUTLINED, selected_icon=Icons.HISTORY, label="History"),
            ft.NavigationDestination(icon=Icons.SETTINGS_OUTLINED, selected_icon=Icons.SETTINGS, label="Settings"),
        ],
    )

    # ---------- INITIAL RENDER: SPLASH -> HOME ----------
    main_content.content = splash_screen
    page.add(main_content)

    def load_home():
        time.sleep(2)
        refresh_recent_list()
        refresh_favorites_list()
        main_content.content = home_screen
        page.navigation_bar = bottom_nav
        page.update()

        msg, success = sync_market_data(db_conn)
        market_status.value = msg
        market_status.color = Colors.GREEN if success else Colors.RED
        page.update()

    threading.Thread(target=load_home, daemon=True).start()


if __name__ == "__main__":
    ft.app(target=main)
