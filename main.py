import flet as ft
import flet.webview as webview
import yfinance as yf
import sqlite3
import threading
from datetime import datetime

# --- Colors & Styling (Groww/StockAI Theme) ---
BG = "#0B0E14"
SURFACE = "#151922"
TEXT_PRIMARY = "#FFFFFF"
TEXT_MUTED = "#8B95A5"
GREEN = "#00D09C"
RED = "#FF5A5F"
GOLD = "#FFD700"
BORDER = "#2A2F3D"

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect("stockapp.db", check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS favorites 
                 (symbol TEXT PRIMARY KEY, latest_price REAL, change_pct REAL, last_updated TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS nifty500_cache (symbol TEXT PRIMARY KEY)''')
    
    # Default top stocks cache for smooth offline/online filtering
    default_stocks = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN', 'ITC', 'TATAMOTORS', 'TATACHEM']
    for s in default_stocks:
        c.execute("INSERT OR IGNORE INTO nifty500_cache (symbol) VALUES (?)", (s,))
    conn.commit()
    return conn, c

conn, cursor = init_db()

# --- Helper Functions ---
def tradingview_embed_url(symbol):
    clean_sym = symbol.replace(".NS", "")
    return f"https://www.tradingview.com/widgetembed/?symbol=NSE:{clean_sym}&interval=D&hidesidetoolbar=1&symboledit=1&theme=dark"

def get_stock_price(symbol):
    try:
        ticker_sym = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
        ticker = yf.Ticker(ticker_sym)
        # Using fast_info for high-performance retrieval
        price = ticker.fast_info.get('last_price')
        prev_close = ticker.fast_info.get('previous_close')
        
        if price and prev_close:
            pct_change = ((price - prev_close) / prev_close) * 100
            return round(price, 2), round(pct_change, 2)
    except Exception as e:
        print(f"Error fetching price for {symbol}: {e}")
    return None, None

def get_company_name(symbol):
    try:
        ticker_sym = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
        ticker = yf.Ticker(ticker_sym)
        info = ticker.info
        return info.get('shortName', symbol)
    except Exception:
        return symbol

# --- Main Application ---
def main(page: ft.Page):
    page.title = "StockAI PRO"
    page.bgcolor = BG
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0

    main_content = ft.Container(expand=True)

    def show_snack(message):
        page.snack_bar = ft.SnackBar(ft.Text(message, color=TEXT_PRIMARY), bgcolor=SURFACE)
        page.snack_bar.open = True
        page.update()

    def render_stock_details(symbol):
        # Initial Loading State UI
        price_text = ft.Text("Fetching Live Price...", size=28, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY)
        pct_text = ft.Text("Please wait...", color=TEXT_MUTED, size=14)
        company_label = ft.Text("Loading details...", color=TEXT_MUTED, size=14)
        
        chart_url = tradingview_embed_url(symbol)
        
        # WebView Live Chart Container
        chart_container = ft.Container(
            expand=True,
            border_radius=16,
            bgcolor=SURFACE,
            border=ft.border.all(1, BORDER),
            height=420,
            content=ft.Column([
                ft.Container(
                    padding=12,
                    content=ft.Text("LIVE CHART (TRADINGVIEW)", size=12, weight=ft.FontWeight.W_700, color=TEXT_MUTED)
                ),
                webview.WebView(
                    url=chart_url,
                    expand=True,
                    on_page_error=lambda e: print("WebView Error")
                )
            ])
        )

        details_view = ft.Container(
            expand=True,
            padding=20,
            bgcolor=BG,
            content=ft.Column(
                [
                    ft.Row([
                        ft.IconButton(ft.icons.ARROW_BACK, icon_color=TEXT_PRIMARY, on_click=lambda e: load_home()),
                        ft.Text(symbol.upper(), size=18, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY, expand=True, text_align=ft.TextAlign.CENTER),
                        ft.IconButton(ft.icons.STAR_BORDER, icon_color=GOLD, on_click=lambda e: toggle_watchlist(symbol))
                    ]),
                    ft.Container(height=10),
                    company_label,
                    price_text,
                    pct_text,
                    ft.Container(height=15),
                    chart_container
                ],
                scroll=ft.ScrollMode.AUTO,
            )
        )
        
        main_content.content = details_view
        page.update()

        # Background thread to fetch live data smoothly without freezing UI
        def background_fetch():
            price, pct = get_stock_price(symbol)
            comp_name = get_company_name(symbol)
            if price is not None:
                color = GREEN if pct >= 0 else RED
                sign = "+" if pct >= 0 else ""
                price_text.value = f"₹{price:,.2f}"
                pct_text.value = f"{sign}{pct}%"
                pct_text.color = color
                company_label.value = comp_name
            else:
                price_text.value = "Data Unavailable"
                pct_text.value = "Check symbol suffix (.NS)"
            page.update()

        threading.Thread(target=background_fetch, daemon=True).start()

    def toggle_watchlist(symbol):
        try:
            cursor.execute("SELECT symbol FROM favorites WHERE symbol=?", (symbol,))
            if cursor.fetchone():
                cursor.execute("DELETE FROM favorites WHERE symbol=?", (symbol,))
                show_snack(f"Removed {symbol} from Watchlist")
            else:
                cursor.execute("INSERT INTO favorites (symbol, last_updated) VALUES (?, ?)", (symbol, datetime.now().strftime("%d %b %Y")))
                conn.commit()
                show_snack(f"Added {symbol} to Watchlist")
        except Exception as e:
            print(e)

    # --- Search Input Component ---
    search_input = ft.TextField(
        hint_text="Search stocks (e.g., RELIANCE, TCS, TATACHEM)...",
        bgcolor=SURFACE,
        border_color=BORDER,
        color=TEXT_PRIMARY,
        border_radius=12,
        on_submit=lambda e: render_stock_details(search_input.value.strip().upper()) if search_input.value else None,
        suffix_icon=ft.icons.SEARCH
    )

    def load_home():
        home_view = ft.Container(
            expand=True,
            padding=20,
            bgcolor=BG,
            content=ft.Column(
                [
                    ft.Row([
                        ft.Text("StockAI PRO", size=24, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY),
                        ft.Text("LIVE MARKET", color=GREEN, size=12, weight=ft.FontWeight.BOLD),
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Container(height=16),
                    search_input,
                    ft.Container(height=24),
                    
                    # Groww Style Quick Sections
                    ft.Text("MARKET MOVERS (NIFTY 500)", size=13, weight=ft.FontWeight.W_700, color=TEXT_MUTED),
                    ft.Container(height=8),
                    ft.Row([
                        ft.ElevatedButton("Top Gainers", bgcolor=GREEN, color=ft.colors.WHITE, on_click=lambda e: show_snack("Loading Gainers...")),
                        ft.ElevatedButton("Top Losers", bgcolor=RED, color=ft.colors.WHITE, on_click=lambda e: show_snack("Loading Losers...")),
                    ], spacing=10),
                    
                    ft.Container(height=20),
                    ft.Text("PROFIT GROWTH SCREENER", size=13, weight=ft.FontWeight.W_700, color=TEXT_MUTED),
                    ft.Container(height=8),
                    ft.ElevatedButton("View Growth Stocks", bgcolor=GOLD, color=ft.colors.BLACK, on_click=lambda e: show_snack("Scanning Growth...")),
                ],
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        main_content.content = home_view
        page.update()

    def load_watchlist():
        cursor.execute("SELECT symbol, latest_price, change_pct FROM favorites")
        rows = cursor.fetchall()
        
        list_items = []
        for row in rows:
            sym, price, pct = row
            col = GREEN if pct and pct >= 0 else RED
            list_items.append(
                ft.ListTile(
                    title=ft.Text(sym, color=TEXT_PRIMARY, weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text(f"₹{price}" if price else "Tap to sync price", color=TEXT_MUTED),
                    trailing=ft.Text(f"{pct}%" if pct else "-", color=col, weight=ft.FontWeight.BOLD),
                    on_click=lambda e, s=sym: render_stock_details(s)
                )
            )
            
        if not list_items:
            list_items.append(ft.Container(padding=20, content=ft.Text("Your watchlist is empty. Search and add stocks.", color=TEXT_MUTED)))

        watch_view = ft.Container(
            expand=True,
            padding=20,
            bgcolor=BG,
            content=ft.Column([
                ft.Text("My Watchlist", size=24, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                ft.Container(height=10),
                ft.Column(list_items, scroll=ft.ScrollMode.AUTO, expand=True)
            ])
        )
        main_content.content = watch_view
        page.update()

    def on_nav_change(e):
        idx = e.control.selected_index
        if idx == 0:
            load_home()
        elif idx == 1:
            main_content.content = ft.Container(padding=20, content=ft.Text("News Section Placeholder", color=TEXT_PRIMARY))
            page.update()
        elif idx == 2:
            load_watchlist()
        elif idx == 3:
            main_content.content = ft.Container(padding=20, content=ft.Text("Settings Section Placeholder", color=TEXT_PRIMARY))
            page.update()

    # --- Bottom Navigation (Groww Style) ---
    bottom_nav = ft.NavigationBar(
        bgcolor=SURFACE,
        selected_index=0,
        on_change=on_nav_change,
        destinations=[
            ft.NavigationDestination(icon=ft.icons.HOME, label="Home"),
            ft.NavigationDestination(icon=ft.icons.ARTICLE, label="News"),
            ft.NavigationDestination(icon=ft.icons.STAR, label="Watchlist"),
            ft.NavigationDestination(icon=ft.icons.SETTINGS, label="Settings"),
        ]
    )

    page.add(
        ft.Column([main_content], expand=True),
        bottom_nav
    )
    load_home()

if __name__ == "__main__":
    ft.app(target=main)
