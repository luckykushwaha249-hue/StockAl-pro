import flet as ft
import sqlite3
import yfinance as yf
import requests
from datetime import datetime
import threading
import time
import shutil
import urllib.parse

# Optional: only needed for the "send PDF to Telegram" feature.
# If it's not installed, the app still works - PDF sending is just skipped
# and a text-only Telegram message is sent instead.
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

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
# PREMIUM DARK THEME (TradingView-style palette)
# ==========================================
BG = "#0B0E14"            # app background - near-black
SURFACE = "#151922"        # cards / rows
SURFACE_ALT = "#1C212C"    # inputs, secondary surfaces
BORDER = "#252B38"         # hairline borders
ACCENT = "#2962FF"         # TradingView blue - primary actions / links
ACCENT_SOFT = "#1E2A4A"    # accent used as a subtle fill
GREEN = "#26A69A"          # gains
RED = "#EF5350"            # losses
TEXT_PRIMARY = "#E8EAED"
TEXT_SECONDARY = "#8B93A7"
TEXT_MUTED = "#5C6470"
GOLD = "#F0B90B"           # premium highlight accent
CANDLE_ICON = getattr(Icons, "CANDLESTICK_CHART", None) or Icons.SHOW_CHART

# ==========================================
# FALLBACK STOCK LIST
# Used only if EVERY live fetch (NSE full list, NSE Nifty500 list, and
# both caches) fails - e.g. the very first run with no internet at all.
# This keeps the app usable even when nothing can be downloaded.
# ==========================================
FALLBACK_UNIVERSE = [
    ("RELIANCE", "Reliance Industries"), ("TCS", "Tata Consultancy Services"),
    ("HDFCBANK", "HDFC Bank"), ("ICICIBANK", "ICICI Bank"), ("INFY", "Infosys"),
    ("BHARTIARTL", "Bharti Airtel"), ("ITC", "ITC Ltd"), ("SBIN", "State Bank of India"),
    ("LT", "Larsen & Toubro"), ("KOTAKBANK", "Kotak Mahindra Bank"),
    ("AXISBANK", "Axis Bank"), ("HCLTECH", "HCL Technologies"), ("MARUTI", "Maruti Suzuki"),
    ("SUNPHARMA", "Sun Pharma"), ("TITAN", "Titan Company"), ("BAJFINANCE", "Bajaj Finance"),
    ("WIPRO", "Wipro Ltd"), ("ULTRACEMCO", "UltraTech Cement"), ("NESTLEIND", "Nestle India"),
    ("TATAMOTORS", "Tata Motors"), ("TATASTEEL", "Tata Steel"), ("NTPC", "NTPC Ltd"),
    ("POWERGRID", "Power Grid Corp"), ("ONGC", "Oil & Natural Gas Corp"), ("COALINDIA", "Coal India"),
    ("HINDUNILVR", "Hindustan Unilever"), ("ASIANPAINT", "Asian Paints"), ("ADANIENT", "Adani Enterprises"),
    ("ADANIPORTS", "Adani Ports & SEZ"), ("BAJAJFINSV", "Bajaj Finserv"), ("CIPLA", "Cipla"),
    ("DRREDDY", "Dr. Reddy's Laboratories"), ("EICHERMOT", "Eicher Motors"), ("GRASIM", "Grasim Industries"),
    ("HDFCLIFE", "HDFC Life Insurance"), ("HINDALCO", "Hindalco Industries"), ("INDIGO", "InterGlobe Aviation"),
    ("JSWSTEEL", "JSW Steel"), ("M&M", "Mahindra & Mahindra"), ("SBILIFE", "SBI Life Insurance"),
    ("SHRIRAMFIN", "Shriram Finance"), ("TATACONSUM", "Tata Consumer Products"), ("TECHM", "Tech Mahindra"),
    ("TRENT", "Trent Ltd"), ("HEROMOTOCO", "Hero MotoCorp"), ("APOLLOHOSP", "Apollo Hospitals"),
    ("BAJAJ-AUTO", "Bajaj Auto"), ("BEL", "Bharat Electronics"), ("JIOFIN", "Jio Financial Services"),
    ("RELAXO", "Relaxo Footwears"),
]

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,application/csv,*/*",
}

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

    # Full NSE universe (~2000 stocks) - replaces the old Nifty500-only cache
    cursor.execute('''CREATE TABLE IF NOT EXISTS nse_universe_cache (
                        symbol TEXT PRIMARY KEY,
                        company_name TEXT)''')

    # Kept for backward-compatible fallback (smaller Nifty500 list)
    cursor.execute('''CREATE TABLE IF NOT EXISTS nifty500_cache (
                        symbol TEXT PRIMARY KEY,
                        company_name TEXT)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS dhan_scrip_cache (
                        symbol TEXT PRIMARY KEY,
                        security_id TEXT)''')

    # Profit Growth screener results: stocks with 8 straight quarters of
    # rising profit AND trading 15%+ below their 52-week high.
    cursor.execute('''CREATE TABLE IF NOT EXISTS profit_growth_stocks (
                        symbol TEXT PRIMARY KEY,
                        company_name TEXT,
                        price REAL,
                        week52_high REAL,
                        pct_below_high REAL,
                        scan_date TEXT)''')

    # Safe migration: add new columns if they don't exist yet
    for coldef in ("change_pct REAL", "last_updated TEXT"):
        try:
            cursor.execute(f"ALTER TABLE favorites ADD COLUMN {coldef}")
        except Exception:
            pass

    for coldef in ("market_cap REAL DEFAULT 0",):
        try:
            cursor.execute(f"ALTER TABLE stock_master ADD COLUMN {coldef}")
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
# FULL NSE MARKET UNIVERSE (~2000 stocks, fetched live, cached, with fallback)
# ==========================================
def _fetch_nifty500_only(conn):
    """Smaller backup list (~500 stocks) - used only if the full ~2000
    stock equity list can't be reached but the Nifty500 endpoint can."""
    cursor = conn.cursor()
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=10)
        resp = session.get(
            "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
            headers=NSE_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")[1:]
        symbols = []
        for line in lines:
            parts = [p.strip().strip('"') for p in line.split(",")]
            if len(parts) >= 3 and parts[2]:
                symbols.append((parts[2], parts[0]))
        if len(symbols) >= 100:
            cursor.execute("DELETE FROM nifty500_cache")
            cursor.executemany(
                "INSERT OR REPLACE INTO nifty500_cache (symbol, company_name) VALUES (?, ?)", symbols,
            )
            cursor.executemany(
                "INSERT OR IGNORE INTO stock_master (symbol, company_name, sector, price) VALUES (?, ?, 'N/A', 0.0)",
                symbols,
            )
            conn.commit()
            return symbols
    except Exception:
        pass
    return None


def fetch_full_market_universe(conn, progress_callback=None):
    """
    Builds the complete searchable stock universe (all NSE main-board
    equities - roughly 2000 stocks, not just the Nifty 500). This is
    what makes ANY stock searchable/addable to the watchlist, not just
    large caps.

    Order of attempts:
      1. NSE's official full equity list (EQUITY_L.csv) -> ~2000 stocks
      2. NSE Nifty 500 list (smaller backup, in case the full list URL
         is temporarily blocked)
      3. Whatever was cached from a previous successful fetch (full
         universe cache, then the smaller Nifty500 cache)
      4. A small fixed FALLBACK_UNIVERSE (only if there's truly no
         internet and no prior cache at all)

    Every symbol found is also upserted into stock_master, so the
    normal search box and the watchlist "add stock" search both pick
    it up automatically - no separate code path needed.
    """
    cursor = conn.cursor()
    try:
        if progress_callback:
            progress_callback("Downloading full NSE stock list (~2000 stocks)...")
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=10)
        resp = session.get(
            "https://nsearchives.nseindia.com/content/equity/EQUITY_L.csv",
            headers=NSE_HEADERS, timeout=25,
        )
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")[1:]
        symbols = []
        for line in lines:
            parts = [p.strip().strip('"') for p in line.split(",")]
            # EQUITY_L.csv columns: SYMBOL, NAME OF COMPANY, SERIES, ...
            if len(parts) >= 2 and parts[0]:
                symbols.append((parts[0], parts[1]))
        if len(symbols) >= 500:
            cursor.execute("DELETE FROM nse_universe_cache")
            cursor.executemany(
                "INSERT OR REPLACE INTO nse_universe_cache (symbol, company_name) VALUES (?, ?)", symbols,
            )
            cursor.executemany(
                "INSERT OR IGNORE INTO stock_master (symbol, company_name, sector, price, market_cap) "
                "VALUES (?, ?, 'N/A', 0.0, 0)",
                symbols,
            )
            conn.commit()
            if progress_callback:
                progress_callback(f"Loaded {len(symbols)} NSE stocks into search index.")
            return symbols
    except Exception:
        pass

    # Attempt 2: smaller Nifty500 backup
    n500 = _fetch_nifty500_only(conn)
    if n500:
        if progress_callback:
            progress_callback(f"Loaded {len(n500)} Nifty500 stocks (full list unavailable).")
        return n500

    # Attempt 3: caches from a previous successful run
    cursor.execute("SELECT symbol, company_name FROM nse_universe_cache")
    cached = cursor.fetchall()
    if cached:
        return cached
    cursor.execute("SELECT symbol, company_name FROM nifty500_cache")
    cached2 = cursor.fetchall()
    if cached2:
        return cached2

    # Last resort: small fixed list
    return FALLBACK_UNIVERSE


def fetch_market_caps(conn, progress_callback=None, limit=None):
    """
    Best-effort market-cap enrichment so search / analytics can be
    ranked by market capitalisation (top-2000-by-market-cap). There is
    no single free bulk market-cap feed for the whole NSE universe, so
    this fetches it stock-by-stock via yfinance. For ~2000 stocks this
    is genuinely slow (can take 15-30+ minutes) - it's a manual action
    in Settings, not something run automatically on every sync. Results
    are cached in stock_master.market_cap and reused until you run it
    again.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT symbol FROM stock_master ORDER BY symbol")
    symbols = [r[0] for r in cursor.fetchall()]
    if limit:
        symbols = symbols[:limit]
    total = len(symbols)
    updated = 0
    for i, sym in enumerate(symbols, 1):
        try:
            t = yf.Ticker(f"{sym}.NS")
            cap = None
            try:
                cap = t.fast_info.get("market_cap")
            except Exception:
                cap = None
            if not cap:
                try:
                    cap = t.info.get("marketCap")
                except Exception:
                    cap = None
            if cap:
                cursor.execute("UPDATE stock_master SET market_cap=? WHERE symbol=?", (float(cap), sym))
                updated += 1
        except Exception:
            continue
        if i % 25 == 0:
            conn.commit()
            if progress_callback:
                progress_callback(f"Market cap scan: {i}/{total} done, {updated} updated...")
    conn.commit()
    set_setting(conn, "market_cap_last_updated", datetime.now().strftime("%d %b %Y, %H:%M"))
    if progress_callback:
        progress_callback(f"Market cap scan complete: {updated}/{total} stocks updated.")
    return updated


def search_stock_db(conn, query):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, company_name, sector, price FROM stock_master "
        "WHERE symbol LIKE ? OR company_name LIKE ? "
        "ORDER BY market_cap DESC, company_name ASC LIMIT 30",
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


def add_to_watchlist(conn, symbol, company_name):
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO favorites (symbol, company_name, latest_price) VALUES (?, ?, 0)",
        (symbol.upper(), company_name),
    )
    conn.commit()


def remove_from_watchlist(conn, symbol):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM favorites WHERE symbol=?", (symbol,))
    conn.commit()


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
    cursor.execute(
        "SELECT symbol, company_name, latest_price, change_pct, last_updated FROM favorites ORDER BY symbol"
    )
    return cursor.fetchall()


def get_last_sync_display(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT date, sync_time FROM market_summary ORDER BY date DESC LIMIT 1")
    row = cursor.fetchone()
    if row:
        return f"Last updated: {row[0]} at {row[1]}"
    return "Not synced yet. Tap 'Update Market Data'."


def is_market_open():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_time <= now <= close_time


def get_available_mover_dates(conn):
    """All dates we have saved gainer/loser data for, newest first -
    powers the date picker so old days' data stays browsable."""
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT date FROM market_movers ORDER BY date DESC")
    return [row[0] for row in cursor.fetchall()]


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


def fetch_dhan_scrip_master(conn):
    """
    Downloads Dhan's instrument master (maps trading symbols to Dhan's
    internal security IDs, required for their market data API) and
    caches it. Only needs to run once in a while - the mapping barely
    changes day to day.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM dhan_scrip_cache")
    if cursor.fetchone()[0] > 0:
        last_fetch = get_setting(conn, "dhan_scrip_fetched_date")
        if last_fetch == datetime.now().strftime("%Y-%m-%d"):
            return True
    try:
        resp = requests.get("https://images.dhan.co/api-data/api-scrip-master-detailed.csv", timeout=30)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        header = [h.strip() for h in lines[0].split(",")]
        idx_symbol = header.index("SEM_TRADING_SYMBOL")
        idx_secid = header.index("SEM_SMST_SECURITY_ID")
        idx_exch = header.index("SEM_EXM_EXCH_ID")
        idx_series = header.index("SEM_SERIES") if "SEM_SERIES" in header else None

        rows = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= max(idx_symbol, idx_secid, idx_exch):
                continue
            if parts[idx_exch].strip() != "NSE":
                continue
            if idx_series is not None and parts[idx_series].strip() not in ("EQ", ""):
                continue
            symbol = parts[idx_symbol].strip()
            secid = parts[idx_secid].strip()
            if symbol and secid.isdigit():
                rows.append((symbol, secid))

        if rows:
            cursor.execute("DELETE FROM dhan_scrip_cache")
            cursor.executemany(
                "INSERT OR REPLACE INTO dhan_scrip_cache (symbol, security_id) VALUES (?, ?)", rows
            )
            set_setting(conn, "dhan_scrip_fetched_date", datetime.now().strftime("%Y-%m-%d"))
            conn.commit()
            return True
    except Exception:
        pass
    cursor.execute("SELECT COUNT(*) FROM dhan_scrip_cache")
    return cursor.fetchone()[0] > 0


def fetch_top_movers_from_dhan(conn, name_lookup):
    """
    PRIORITY 1: Uses the authenticated DhanHQ market data API (requires
    a free Dhan account + API access token, set in Settings) to fetch
    OHLC data for the whole stock universe and compute Top 10
    Gainers/Losers. This is the most reliable source since it's an
    official, authenticated broker API.
    Returns (gainers, losers, data_date) or (None, None, None).
    """
    client_id = get_setting(conn, "dhan_client_id")
    access_token = get_setting(conn, "dhan_access_token")
    if not client_id or not access_token:
        return None, None, None

    try:
        if not fetch_dhan_scrip_master(conn):
            return None, None, None

        cursor = conn.cursor()
        cursor.execute("SELECT symbol, security_id FROM dhan_scrip_cache")
        sec_id_map = {row[0]: row[1] for row in cursor.fetchall()}

        universe = list(name_lookup.items())
        sec_ids = []
        secid_to_symbol = {}
        for symbol, _ in universe:
            secid = sec_id_map.get(symbol)
            if secid:
                sec_ids.append(secid)
                secid_to_symbol[secid] = symbol

        if not sec_ids:
            return None, None, None

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": access_token,
            "client-id": client_id,
        }

        movers = []
        batch_size = 900
        for i in range(0, len(sec_ids), batch_size):
            batch = [int(s) for s in sec_ids[i:i + batch_size]]
            resp = requests.post(
                "https://api.dhan.co/v2/marketfeed/ohlc",
                headers=headers,
                json={"NSE_EQ": batch},
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            nse_data = payload.get("data", {}).get("NSE_EQ", {})
            for secid_str, info in nse_data.items():
                symbol = secid_to_symbol.get(secid_str)
                if not symbol:
                    continue
                try:
                    last_price = float(info.get("last_price", 0))
                    prev_close = float(info.get("ohlc", {}).get("close", 0))
                    if last_price <= 0 or prev_close <= 0:
                        continue
                    pct_change = round((last_price - prev_close) / prev_close * 100, 2)
                    movers.append({
                        "symbol": symbol,
                        "company_name": name_lookup.get(symbol, symbol),
                        "price": round(last_price, 2),
                        "pct_change": pct_change,
                    })
                except Exception:
                    continue

        if not movers:
            return None, None, None

        gainers = sorted(movers, key=lambda m: m["pct_change"], reverse=True)[:10]
        losers = sorted(movers, key=lambda m: m["pct_change"])[:10]
        data_date = datetime.now().strftime("%Y-%m-%d")
        return gainers, losers, data_date
    except Exception:
        return None, None, None


def fetch_top_movers_from_nse(name_lookup):
    """
    PRIORITY 2: Fetches pre-computed top gainers/losers directly from
    NSE's live market-movers endpoint (the same data source behind
    nseindia.com and most broker apps' 'Top Gainers/Losers' pages).
    Much faster than downloading the whole universe ourselves.
    Returns (gainers, losers, data_date) or (None, None, None).
    """
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com/market-data/top-gainers-losers", headers=NSE_HEADERS, timeout=10)

        resp_g = session.get(
            "https://www.nseindia.com/api/live-analysis-variations?index=gainers",
            headers=NSE_HEADERS, timeout=15,
        )
        resp_g.raise_for_status()
        data_g = resp_g.json()

        resp_l = session.get(
            "https://www.nseindia.com/api/live-analysis-variations?index=loosers",
            headers=NSE_HEADERS, timeout=15,
        )
        resp_l.raise_for_status()
        data_l = resp_l.json()

        def extract(payload):
            # "allSec" = All Securities on NSE, the broadest live-movers list
            # NSE publishes for this feed.
            rows = payload.get("allSec", {}).get("data", [])
            out = []
            for r in rows:
                try:
                    sym = r["symbol"]
                    out.append({
                        "symbol": sym,
                        "company_name": name_lookup.get(sym, sym),
                        "price": float(r.get("ltp", 0)),
                        "pct_change": float(r.get("perChange", 0)),
                    })
                except Exception:
                    continue
            return out

        gainers = sorted(extract(data_g), key=lambda m: m["pct_change"], reverse=True)
        losers = sorted(extract(data_l), key=lambda m: m["pct_change"])

        if not gainers or not losers:
            return None, None, None

        data_date = datetime.now().strftime("%Y-%m-%d")
        return gainers[:10], losers[:10], data_date
    except Exception:
        return None, None, None


def perform_full_market_sync(conn, progress_callback=None):
    """
    Refreshes EVERYTHING in the app in one go: the full ~2000 stock
    search index, Top 10 Gainers / Losers (saved per-date so history
    stays browsable), the watchlist, and the news feed.

    Fallback chain (unchanged priority, kept in this order):
      1. Dhan API (authenticated broker feed - fastest & most reliable)
      2. NSE live top-movers endpoint
      3. Full universe scan via yfinance (slowest, last resort)
    """
    try:
        if progress_callback:
            progress_callback("Refreshing full stock search index...")
        universe = fetch_full_market_universe(conn, progress_callback)
        name_lookup = {sym: name for sym, name in universe}

        if progress_callback:
            progress_callback("Trying Dhan API...")
        gainers, losers, data_date = fetch_top_movers_from_dhan(conn, name_lookup)
        source_used = "Dhan"

        if not gainers or not losers:
            if progress_callback:
                progress_callback("Trying NSE live data...")
            gainers, losers, data_date = fetch_top_movers_from_nse(name_lookup)
            source_used = "NSE"

        if not gainers or not losers:
            source_used = "Full Scan"
            if progress_callback:
                progress_callback("Fast methods unavailable, doing a full scan instead (this is slower)...")
            gainers, losers, data_date = _scan_full_universe_for_movers(conn, universe, progress_callback)

        if not gainers or not losers or not data_date:
            return "Sync Failed: No data received (check internet connection)", False

        cursor = conn.cursor()
        cursor.execute("DELETE FROM market_movers WHERE date=?", (data_date,))
        for rank, m in enumerate(gainers, 1):
            cursor.execute(
                "INSERT INTO market_movers (date, type, rank, symbol, company_name, price, pct_change) "
                "VALUES (?,?,?,?,?,?,?)",
                (data_date, "gainer", rank, m["symbol"], m["company_name"], m["price"], m["pct_change"]),
            )
        for rank, m in enumerate(losers, 1):
            cursor.execute(
                "INSERT INTO market_movers (date, type, rank, symbol, company_name, price, pct_change) "
                "VALUES (?,?,?,?,?,?,?)",
                (data_date, "loser", rank, m["symbol"], m["company_name"], m["price"], m["pct_change"]),
            )

        movers_by_symbol = {m["symbol"]: m for m in gainers + losers}
        for m in movers_by_symbol.values():
            cursor.execute("UPDATE stock_master SET price=? WHERE symbol=?", (m["price"], m["symbol"]))

        # Update watchlist with latest price / change
        cursor.execute("SELECT symbol FROM favorites")
        fav_symbols = [row[0] for row in cursor.fetchall()]
        sync_timestamp = datetime.now().strftime("%d %b %Y, %H:%M")

        missing_fav_symbols = [s for s in fav_symbols if s not in movers_by_symbol]
        if missing_fav_symbols:
            if progress_callback:
                progress_callback("Updating your watchlist stocks...")
            try:
                fav_tickers = [f"{s}.NS" for s in missing_fav_symbols]
                fav_data = yf.download(tickers=fav_tickers, period="5d", group_by="ticker", threads=True, progress=False)
                for s in missing_fav_symbols:
                    ns = f"{s}.NS"
                    try:
                        if len(missing_fav_symbols) > 1:
                            try:
                                closes = fav_data[ns]["Close"].dropna()
                            except Exception:
                                closes = fav_data["Close"][ns].dropna()
                        else:
                            closes = fav_data["Close"].dropna()
                        if len(closes) >= 2:
                            last_close = float(closes.iloc[-1])
                            prev_close = float(closes.iloc[-2])
                            pct = round((last_close - prev_close) / prev_close * 100, 2)
                            movers_by_symbol[s] = {"price": round(last_close, 2), "pct_change": pct}
                    except Exception:
                        continue
            except Exception:
                pass

        for symbol in fav_symbols:
            m = movers_by_symbol.get(symbol)
            if m:
                cursor.execute(
                    "UPDATE favorites SET latest_price=?, change_pct=?, last_updated=? WHERE symbol=?",
                    (m["price"], m["pct_change"], sync_timestamp, symbol),
                )

        # News feed: union of gainers / losers (no duplicates)
        seen = set()
        news_entries = []
        for lst in (gainers, losers):
            for m in lst:
                if m["symbol"] not in seen:
                    seen.add(m["symbol"])
                    news_entries.append(m)

        cursor.execute("DELETE FROM news_items WHERE date=?", (data_date,))
        for m in news_entries:
            cursor.execute(
                "INSERT INTO news_items (symbol, company_name, date, pct_change) VALUES (?,?,?,?)",
                (m["symbol"], m["company_name"], data_date, m["pct_change"]),
            )
        cursor.execute("DELETE FROM news_items WHERE date < date('now', '-7 days')")

        cursor.execute(
            "INSERT OR REPLACE INTO market_summary (date, value, sync_time) VALUES (?, ?, ?)",
            (data_date, gainers[0]["price"] if gainers else 0, sync_timestamp),
        )
        conn.commit()

        status = "Live" if is_market_open() else "Closed"
        return f"Synced ({status}/{source_used}) - data as of {data_date}", True
    except Exception as e:
        return f"Sync Failed: {e}", False


def _scan_full_universe_for_movers(conn, universe, progress_callback=None):
    """
    PRIORITY 3 (last resort): downloads price data for the whole stock
    universe ourselves and computes top gainers/losers manually.
    Slower (can take a few minutes for ~2000 stocks).
    """
    symbols_only = [s for s, _ in universe]
    name_lookup = {s: n for s, n in universe}
    tickers = [f"{s}.NS" for s in symbols_only]

    if progress_callback:
        progress_callback(f"Downloading price data for {len(tickers)} stocks (this can take a few minutes)...")

    movers = []
    data_date = None
    batch_size = 250
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        try:
            data = yf.download(tickers=batch, period="5d", group_by="ticker", threads=True, progress=False)
        except Exception:
            continue

        for ns_symbol in batch:
            symbol = ns_symbol.replace(".NS", "")
            try:
                try:
                    closes = data[ns_symbol]["Close"].dropna()
                except Exception:
                    closes = data["Close"][ns_symbol].dropna()
                if len(closes) < 2:
                    continue
                last_close = float(closes.iloc[-1])
                prev_close = float(closes.iloc[-2])
                pct_change = round((last_close - prev_close) / prev_close * 100, 2)

                if data_date is None:
                    data_date = closes.index[-1].strftime("%Y-%m-%d")

                movers.append({
                    "symbol": symbol,
                    "company_name": name_lookup.get(symbol, symbol),
                    "price": round(last_close, 2),
                    "pct_change": pct_change,
                })
            except Exception:
                continue

        if progress_callback:
            progress_callback(f"Processed {min(i + batch_size, len(tickers))}/{len(tickers)} stocks...")
        time.sleep(0.2)

    if not movers or data_date is None:
        return None, None, None

    gainers = sorted(movers, key=lambda m: m["pct_change"], reverse=True)[:10]
    losers = sorted(movers, key=lambda m: m["pct_change"])[:10]
    return gainers, losers, data_date


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
# TELEGRAM NOTIFICATIONS (gainers/losers auto-sent after every sync)
# ==========================================
def send_telegram_message(bot_token, chat_id, text):
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        return resp.ok
    except Exception:
        return False


def send_telegram_document(bot_token, chat_id, file_path, caption=""):
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption},
                files={"document": f},
                timeout=30,
            )
        return resp.ok
    except Exception:
        return False


def fetch_telegram_chat_id(bot_token):
    """
    Telegram never lets a bot message someone using just a phone number
    (spam protection) - this is a platform rule that applies to every
    bot ever made, not something specific to this app. The one-time
    workaround: the user sends any message (e.g. /start) to their own
    bot once, and we read that message back to learn their chat id.
    After that, no further manual step is ever needed again.
    """
    try:
        resp = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("result", [])
        if not results:
            return None, "No messages found yet. Send /start to your bot on Telegram first, then tap this again."
        last = results[-1]
        chat = last.get("message", {}).get("chat", {})
        chat_id = chat.get("id")
        name = chat.get("first_name") or chat.get("username") or "your account"
        if chat_id:
            return str(chat_id), f"Connected! Updates will be sent to {name} on Telegram."
        return None, "Couldn't read a chat id from Telegram's response."
    except Exception as e:
        return None, f"Failed: {e}"


def build_movers_text_message(gainers, losers, data_date):
    lines = ["<b>StockAI Pro - Market Movers</b>", f"Date: {data_date}", "", "<b>Top Gainers</b>"]
    for i, m in enumerate(gainers[:10], 1):
        lines.append(f"{i}. {m['symbol']} - Rs.{m['price']:,.2f} ({m['pct_change']:+.2f}%)")
    lines.append("")
    lines.append("<b>Top Losers</b>")
    for i, m in enumerate(losers[:10], 1):
        lines.append(f"{i}. {m['symbol']} - Rs.{m['price']:,.2f} ({m['pct_change']:+.2f}%)")
    return "\n".join(lines)


def generate_movers_pdf(gainers, losers, data_date, path="movers_report.pdf"):
    if not REPORTLAB_AVAILABLE:
        return None
    try:
        c = pdf_canvas.Canvas(path, pagesize=A4)
        width, height = A4
        y = height - 50

        def new_page_if_needed(cur_y):
            if cur_y < 60:
                c.showPage()
                return height - 50
            return cur_y

        c.setFont("Helvetica-Bold", 16)
        c.drawString(40, y, "StockAI Pro - Market Movers")
        y -= 20
        c.setFont("Helvetica", 11)
        c.drawString(40, y, f"Date: {data_date}")
        y -= 30

        c.setFont("Helvetica-Bold", 13)
        c.drawString(40, y, "Top Gainers")
        y -= 20
        c.setFont("Helvetica", 10)
        for i, m in enumerate(gainers[:10], 1):
            y = new_page_if_needed(y)
            c.drawString(40, y, f"{i}. {m['symbol']} - {m['company_name'][:28]}")
            c.drawString(320, y, f"Rs.{m['price']:,.2f}  ({m['pct_change']:+.2f}%)")
            y -= 16

        y -= 15
        y = new_page_if_needed(y)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(40, y, "Top Losers")
        y -= 20
        c.setFont("Helvetica", 10)
        for i, m in enumerate(losers[:10], 1):
            y = new_page_if_needed(y)
            c.drawString(40, y, f"{i}. {m['symbol']} - {m['company_name'][:28]}")
            c.drawString(320, y, f"Rs.{m['price']:,.2f}  ({m['pct_change']:+.2f}%)")
            y -= 16

        c.save()
        return path
    except Exception:
        return None


def send_telegram_movers_update(conn, progress_callback=None):
    """
    Sends the latest saved gainers/losers as a Telegram text message,
    plus a PDF report if reportlab is installed, to the chat id saved
    in Settings. Does nothing (silently) if Telegram isn't configured -
    this is an optional feature, not required for the app to work.
    """
    bot_token = get_setting(conn, "telegram_bot_token")
    chat_id = get_setting(conn, "telegram_chat_id")
    if not bot_token or not chat_id:
        return

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(date) FROM market_movers")
        row = cursor.fetchone()
        data_date = row[0] if row else None
        if not data_date:
            return

        gainer_rows, _ = get_market_movers(conn, "gainer", data_date)
        loser_rows, _ = get_market_movers(conn, "loser", data_date)
        gainers = [{"symbol": r[1], "company_name": r[2], "price": r[3], "pct_change": r[4]} for r in gainer_rows]
        losers = [{"symbol": r[1], "company_name": r[2], "price": r[3], "pct_change": r[4]} for r in loser_rows]
        if not gainers and not losers:
            return

        if progress_callback:
            progress_callback("Sending update to Telegram...")

        text = build_movers_text_message(gainers, losers, data_date)
        send_telegram_message(bot_token, chat_id, text)

        pdf_path = generate_movers_pdf(gainers, losers, data_date)
        if pdf_path:
            send_telegram_document(bot_token, chat_id, pdf_path, caption=f"Market Movers - {data_date}")

        if progress_callback:
            progress_callback("Telegram update sent.")
    except Exception:
        pass


# ==========================================
# PROFIT GROWTH SCREENER
# Condition 1: profit has risen for 8 straight quarters (last ~2 years)
# Condition 2: currently trading 15%+ below its 52-week high
# ==========================================
def get_quarterly_profit_trend(symbol, quarters_needed=8):
    """
    Returns the last `quarters_needed` quarterly net-income figures in
    oldest-to-newest order, or None if that much history isn't available
    from Yahoo Finance for this stock (common for smaller/newer listings).
    """
    try:
        t = yf.Ticker(f"{symbol}.NS")
        stmt = None
        try:
            stmt = t.quarterly_income_stmt
        except Exception:
            stmt = None
        if stmt is None or stmt.empty:
            try:
                stmt = t.quarterly_financials
            except Exception:
                return None
        if stmt is None or stmt.empty:
            return None

        row = None
        for label in ("Net Income", "NetIncome", "Net Income Common Stockholders"):
            if label in stmt.index:
                row = stmt.loc[label]
                break
        if row is None:
            return None

        # yfinance returns columns newest-first; sort oldest -> newest
        row = row.dropna().sort_index()
        if len(row) < quarters_needed:
            return None
        values = [float(v) for v in row.iloc[-quarters_needed:]]
        return values
    except Exception:
        return None


def is_profit_growing_every_quarter(profit_values):
    """Condition 1: profit must have increased every single quarter."""
    if not profit_values or len(profit_values) < 2:
        return False
    return all(profit_values[i] < profit_values[i + 1] for i in range(len(profit_values) - 1))


def get_52_week_position(symbol):
    """Returns (current_price, week52_high, pct_below_high) or (None, None, None)."""
    try:
        t = yf.Ticker(f"{symbol}.NS")
        hist = t.history(period="1y")
        if hist is None or hist.empty:
            return None, None, None
        week52_high = float(hist["High"].max())
        current_price = float(hist["Close"].dropna().iloc[-1])
        if week52_high <= 0:
            return None, None, None
        pct_below = round((week52_high - current_price) / week52_high * 100, 2)
        return round(current_price, 2), round(week52_high, 2), pct_below
    except Exception:
        return None, None, None


def scan_profit_growth_universe(conn, progress_callback=None):
    """
    Scans every stock in the ~2000-stock universe (stock_master) against
    both conditions. This is heavy - two Yahoo Finance calls per stock -
    so it's meant to run once a day in the background, not on every tap.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, company_name FROM stock_master ORDER BY symbol")
    universe = cursor.fetchall()
    total = len(universe)
    matched = []

    for i, (symbol, company_name) in enumerate(universe, 1):
        try:
            profits = get_quarterly_profit_trend(symbol, 8)
            if not is_profit_growing_every_quarter(profits):
                continue
            price, week52_high, pct_below = get_52_week_position(symbol)
            if price is None or pct_below is None or pct_below < 15:
                continue
            matched.append((symbol, company_name, price, week52_high, pct_below))
        except Exception:
            continue

        if progress_callback and i % 20 == 0:
            progress_callback(f"Profit Growth scan: {i}/{total} scanned, {len(matched)} matched so far...")

    matched.sort(key=lambda m: m[4], reverse=True)
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("DELETE FROM profit_growth_stocks")
    for symbol, company_name, price, week52_high, pct_below in matched:
        cursor.execute(
            "INSERT INTO profit_growth_stocks (symbol, company_name, price, week52_high, pct_below_high, scan_date) "
            "VALUES (?,?,?,?,?,?)",
            (symbol, company_name, price, week52_high, pct_below, today),
        )
    conn.commit()
    set_setting(conn, "profit_growth_last_scan_date", today)
    if progress_callback:
        progress_callback(f"Profit Growth scan complete: {len(matched)} stocks matched both conditions.")
    return len(matched)


def get_profit_growth_stocks(conn):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, company_name, price, week52_high, pct_below_high, scan_date "
        "FROM profit_growth_stocks ORDER BY pct_below_high DESC"
    )
    return cursor.fetchall()


def run_profit_growth_scan_if_due(conn, progress_callback=None):
    """
    Runs the (slow) full-universe scan at most once per day, triggered
    from 'Update Market Data' - so the quick daily sync stays fast, and
    this heavier scan happens once in the background afterwards.
    """
    last_scan = get_setting(conn, "profit_growth_last_scan_date")
    today = datetime.now().strftime("%Y-%m-%d")
    if last_scan == today:
        return
    scan_profit_growth_universe(conn, progress_callback)


# ==========================================
# LIVE INDEX QUOTES (NIFTY 50 / BANK NIFTY) - Groww-style ticker bar
# ==========================================
def fetch_index_quotes():
    """
    Live price when the market's open, last close when it's shut -
    either way this always returns the most recent available quote.
    Returns {"NIFTY 50": (price, change, pct_change), "BANK NIFTY": (...)}
    with (None, None, None) for any index that couldn't be fetched.
    """
    result = {}
    for label, ticker in (("NIFTY 50", "^NSEI"), ("BANK NIFTY", "^NSEBANK")):
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            closes = hist["Close"].dropna()
            if len(closes) >= 2:
                last = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                change = last - prev
                pct = (change / prev * 100) if prev else 0.0
                result[label] = (round(last, 2), round(change, 2), round(pct, 2))
            else:
                result[label] = (None, None, None)
        except Exception:
            result[label] = (None, None, None)
    return result


# ==========================================
# TRADINGVIEW CHART EMBED (falls back gracefully if WebView isn't available)
# ==========================================
def build_tradingview_chart_url(symbol):
    tv_symbol = urllib.parse.quote(f"NSE:{symbol}")
    html = f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>html,body{{margin:0;padding:0;height:100%;background:#0B0E14;}}</style></head>
<body><div class="tradingview-widget-container" style="height:100%;width:100%">
<div id="tv_chart_container" style="height:100%;width:100%"></div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
new TradingView.widget({{
  "autosize": true,
  "symbol": "NSE:{symbol}",
  "interval": "D",
  "timezone": "Asia/Kolkata",
  "theme": "dark",
  "style": "1",
  "locale": "in",
  "toolbar_bg": "#151922",
  "enable_publishing": false,
  "hide_top_toolbar": false,
  "save_image": false,
  "container_id": "tv_chart_container"
}});
</script>
</div></body></html>"""
    return "data:text/html;charset=utf-8," + urllib.parse.quote(html)


def tradingview_web_url(symbol):
    return f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"


# ==========================================
# 2. MAIN APPLICATION
# ==========================================
def main(page: ft.Page):
    page.title = "StockAI Pro"
    page.padding = 0
    page.bgcolor = BG

    db_conn = init_db()

    stored_theme = get_setting(db_conn, "theme_mode", "system")
    if stored_theme == "light":
        page.theme_mode = ft.ThemeMode.LIGHT
    elif stored_theme == "dark":
        page.theme_mode = ft.ThemeMode.DARK
    else:
        page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme_seed="blue", use_material3=True)
    page.dark_theme = ft.Theme(color_scheme_seed="blue", use_material3=True)

    main_content = ft.Container(expand=True, bgcolor=BG)

    # ---------- CLIPBOARD COPY HELPER ----------
    def copy_to_clipboard(text, label="Text"):
        page.set_clipboard(text)
        page.snack_bar = ft.SnackBar(
            content=ft.Text(f"{label} copied to clipboard", color=TEXT_PRIMARY),
            bgcolor=SURFACE_ALT, duration=1200,
        )
        page.snack_bar.open = True
        page.update()

    # ---------- SPLASH SCREEN ----------
    splash_screen = ft.Container(
        expand=True,
        bgcolor=BG,
        alignment=ft.alignment.center,
        content=ft.Column(
            [
                ft.Container(
                    content=ft.Icon(CANDLE_ICON, size=64, color=ACCENT),
                    padding=22, bgcolor=SURFACE, border_radius=24,
                    border=ft.border.all(1, BORDER),
                ),
                ft.Container(height=18),
                ft.Text("StockAI PRO", size=34, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY,
                        style=ft.TextStyle(letter_spacing=2)),
                ft.Text("PERSONAL AI STOCK RESEARCH TERMINAL", size=11, color=TEXT_SECONDARY,
                        style=ft.TextStyle(letter_spacing=2)),
                ft.Container(height=40),
                ft.ProgressBar(width=160, color=ACCENT, bgcolor=SURFACE_ALT),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        ),
    )

    # ---------- ERROR SCREEN ----------
    def show_error_screen(message):
        main_content.content = ft.Container(
            expand=True,
            alignment=ft.alignment.center,
            padding=20,
            content=ft.Column(
                [
                    ft.Icon(Icons.ERROR_OUTLINE, size=60, color=Colors.RED),
                    ft.Text("Something went wrong", size=20, weight=ft.FontWeight.BOLD),
                    ft.Text(str(message), size=12, color=Colors.GREY_600, selectable=True),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        page.update()

    # ---------- STOCK DETAILS PAGE ----------
    def show_stock_details(symbol, company_name, sector, price):
        is_fav = any(f[0] == symbol for f in get_favorites(db_conn))
        fav_icon = ft.Icon(Icons.STAR if is_fav else Icons.STAR_BORDER, color=GOLD if is_fav else TEXT_MUTED)

        def go_back(e):
            main_content.content = home_screen
            page.update()

        def on_fav_click(e):
            added = toggle_favorite(db_conn, symbol, company_name, price)
            fav_icon.name = Icons.STAR if added else Icons.STAR_BORDER
            fav_icon.color = GOLD if added else TEXT_MUTED
            refresh_watchlist_list()
            page.update()

        def on_copy_click(e):
            copy_to_clipboard(f"{symbol} - {company_name} - Rs.{price:,.2f}", "Stock info")

        details_page = ft.Container(
            padding=20,
            bgcolor=BG,
            content=ft.Column([
                ft.Row([
                    ft.IconButton(Icons.ARROW_BACK, icon_color=TEXT_PRIMARY, on_click=go_back),
                    ft.Text(symbol, size=22, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY),
                    ft.Row([
                        ft.IconButton(Icons.COPY, icon_color=TEXT_SECONDARY, on_click=on_copy_click, tooltip="Copy stock info"),
                        ft.IconButton(content=fav_icon, on_click=on_fav_click, tooltip="Add/Remove Watchlist"),
                    ], spacing=0),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Container(height=10),
                ft.Container(
                    bgcolor=SURFACE, border_radius=18, border=ft.border.all(1, BORDER),
                    padding=22,
                    content=ft.Column([
                        ft.Container(
                            padding=ft.padding.symmetric(horizontal=8, vertical=3), border_radius=6,
                            bgcolor=ACCENT_SOFT,
                            content=ft.Text(sector, color=ACCENT, size=11, weight=ft.FontWeight.W_600),
                        ),
                        ft.Container(height=10),
                        ft.Text(f"Rs.{price:,.2f}" if price else "Not synced yet",
                                size=34, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY),
                        ft.Text(company_name, color=TEXT_SECONDARY, size=14),
                    ]),
                ),
                ft.Container(height=16),
                ft.ElevatedButton(
                    "Read News on Google",
                    icon=Icons.OPEN_IN_NEW,
                    color=Colors.WHITE, bgcolor=ACCENT,
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12), padding=16, elevation=0),
                    on_click=lambda e: page.launch_url(google_news_url(company_name)),
                ),
                ft.Container(height=16),
                ft.Text("LIVE CHART", size=12, weight=ft.FontWeight.W_700, color=TEXT_MUTED,
                        style=ft.TextStyle(letter_spacing=1.5)),
                ft.Container(height=8),
                build_chart_container(symbol),
            ], scroll=ft.ScrollMode.AUTO),
        )
        main_content.content = details_page
        page.update()

    def build_chart_container(symbol):
        """TradingView's free embeddable widget shown in an in-app WebView.
        Falls back to an 'Open Chart' browser button if WebView isn't
        supported on this build/device."""
        try:
            chart_view = ft.WebView(
                url=build_tradingview_chart_url(symbol),
                on_page_started=lambda e: None,
            )
            return ft.Container(
                height=420, border_radius=16, border=ft.border.all(1, BORDER),
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                content=chart_view,
            )
        except Exception:
            return ft.Container(
                height=160, border_radius=16, bgcolor=SURFACE, border=ft.border.all(1, BORDER),
                alignment=ft.alignment.center,
                content=ft.Column(
                    [
                        ft.Icon(Icons.SHOW_CHART, size=36, color=TEXT_MUTED),
                        ft.Text("In-app chart isn't supported on this device.", size=12, color=TEXT_MUTED),
                        ft.ElevatedButton(
                            "Open Chart on TradingView",
                            icon=Icons.OPEN_IN_NEW,
                            color=Colors.WHITE, bgcolor=ACCENT,
                            style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12), elevation=0),
                            on_click=lambda e, s=symbol: page.launch_url(tradingview_web_url(s)),
                        ),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=8,
                ),
            )


    # ---------- HOME SCREEN ----------
    search_input = ft.TextField(
        hint_text="Search Stock (e.g. RELIANCE, TCS, RELAXO)",
        hint_style=ft.TextStyle(color=TEXT_MUTED),
        prefix_icon=Icons.SEARCH,
        border_radius=14,
        filled=True,
        fill_color=SURFACE_ALT,
        border_color=BORDER,
        focused_border_color=ACCENT,
        color=TEXT_PRIMARY,
        cursor_color=ACCENT,
        height=54,
        text_size=15,
    )

    result_column = ft.Column(spacing=6)
    recent_list = ft.Column()

    def refresh_recent_list():
        recent_list.controls.clear()
        recents = get_recent_searches(db_conn)
        if not recents:
            recent_list.controls.append(
                ft.Container(
                    content=ft.Row([
                        ft.Icon(Icons.HISTORY, color=TEXT_MUTED, size=18),
                        ft.Text("No recent searches", color=TEXT_MUTED, size=13),
                    ]),
                    padding=10,
                )
            )
        else:
            for q in recents:
                recent_list.controls.append(
                    ft.Container(
                        border_radius=10,
                        bgcolor=SURFACE,
                        margin=ft.margin.only(bottom=6),
                        content=ft.ListTile(
                            leading=ft.Icon(Icons.HISTORY, color=TEXT_SECONDARY, size=18),
                            title=ft.Text(q, color=TEXT_PRIMARY, size=14),
                            on_click=lambda e, q=q: run_search(q),
                        ),
                    )
                )

    def run_search(query):
        results = search_stock_db(db_conn, query)
        result_column.controls.clear()
        if results:
            add_recent_search(db_conn, query)
            refresh_recent_list()
            for symbol, company_name, sector, price in results:
                up = True
                result_column.controls.append(
                    ft.Container(
                        bgcolor=SURFACE,
                        border_radius=12,
                        border=ft.border.all(1, BORDER),
                        content=ft.ListTile(
                            leading=ft.Container(
                                width=38, height=38, border_radius=10, bgcolor=ACCENT_SOFT,
                                alignment=ft.alignment.center,
                                content=ft.Icon(Icons.SHOW_CHART, color=ACCENT, size=18),
                            ),
                            title=ft.Text(symbol, color=TEXT_PRIMARY, weight=ft.FontWeight.BOLD, size=14),
                            subtitle=ft.Text(company_name, color=TEXT_SECONDARY, size=12),
                            trailing=ft.Text(
                                f"Rs.{price:,.2f}" if price else "--",
                                color=TEXT_PRIMARY, weight=ft.FontWeight.W_600, size=13,
                            ),
                            on_click=lambda e, s=symbol, c=company_name, sec=sector, p=price: show_stock_details(s, c, sec, p),
                        ),
                    )
                )
        else:
            result_column.controls.append(ft.Text(f"No results found for '{query}'", color=TEXT_MUTED, size=13))
        page.update()

    def handle_search(e):
        query = (search_input.value or "").strip()
        if query:
            run_search(query)

    search_input.on_submit = handle_search

    market_status_text = ft.Text("Connecting...", size=11, color=TEXT_MUTED)
    nifty_price_text = ft.Text("--", size=16, weight=ft.FontWeight.W_800, color=TEXT_PRIMARY)
    nifty_change_text = ft.Text("", size=11, color=TEXT_MUTED)
    banknifty_price_text = ft.Text("--", size=16, weight=ft.FontWeight.W_800, color=TEXT_PRIMARY)
    banknifty_change_text = ft.Text("", size=11, color=TEXT_MUTED)

    def _index_tile(label, price_ctrl, change_ctrl):
        return ft.Container(
            expand=True, bgcolor=SURFACE, border_radius=14, border=ft.border.all(1, BORDER),
            padding=12,
            content=ft.Column(
                [
                    ft.Text(label, size=11, color=TEXT_MUTED, weight=ft.FontWeight.W_600),
                    price_ctrl,
                    change_ctrl,
                ],
                spacing=2,
            ),
        )

    index_ticker_row = ft.Row(
        [
            _index_tile("NIFTY 50", nifty_price_text, nifty_change_text),
            _index_tile("BANK NIFTY", banknifty_price_text, banknifty_change_text),
        ],
        spacing=10,
    )

    # ---------- AUTOMATIC LIVE REFRESH (only runs while the app is open) ----------
    auto_refresh_stop = {"stop": False}

    def update_index_ticker():
        quotes = fetch_index_quotes()
        for label, price_ctrl, change_ctrl in (
            ("NIFTY 50", nifty_price_text, nifty_change_text),
            ("BANK NIFTY", banknifty_price_text, banknifty_change_text),
        ):
            price, change, pct = quotes.get(label, (None, None, None))
            if price is not None:
                price_ctrl.value = f"{price:,.2f}"
                up = change >= 0
                change_ctrl.value = f"{change:+,.2f} ({pct:+.2f}%)"
                change_ctrl.color = GREEN if up else RED
            else:
                price_ctrl.value = "--"
                change_ctrl.value = "Unavailable"
                change_ctrl.color = TEXT_MUTED
        market_status_text.value = (
            f"LIVE - updated {datetime.now().strftime('%H:%M:%S')}" if is_market_open()
            else f"Market Closed - last close, updated {datetime.now().strftime('%H:%M:%S')}"
        )
        market_status_text.color = GREEN if is_market_open() else TEXT_MUTED
        page.update()

    def auto_refresh_loop():
        last_full_sync = 0.0
        while not auto_refresh_stop["stop"]:
            try:
                update_index_ticker()
            except Exception:
                pass

            try:
                now_ts = time.time()
                interval = 60 if is_market_open() else 300
                if now_ts - last_full_sync > interval:
                    msg, success = perform_full_market_sync(db_conn, None)
                    if success:
                        refresh_watchlist_list()
                        refresh_news_screen()
                        refresh_analytics_screen()
                        page.update()
                        send_telegram_movers_update(db_conn, None)

                        def pg_progress(m):
                            profit_growth_status_text.value = m
                            page.update()
                        threading.Thread(
                            target=lambda: run_profit_growth_scan_if_due(db_conn, pg_progress),
                            daemon=True,
                        ).start()
                    last_full_sync = now_ts
            except Exception:
                pass

            for _ in range(15):
                if auto_refresh_stop["stop"]:
                    break
                time.sleep(1)

    def stop_auto_refresh(e=None):
        auto_refresh_stop["stop"] = True

    page.on_disconnect = stop_auto_refresh

    home_screen = ft.Container(
        expand=True,
        padding=20,
        bgcolor=BG,
        content=ft.Column(
            [
                ft.Container(height=14),
                ft.Row([
                    ft.Container(
                        width=44, height=44, border_radius=12, bgcolor=SURFACE,
                        border=ft.border.all(1, BORDER), alignment=ft.alignment.center,
                        content=ft.Icon(CANDLE_ICON, color=ACCENT, size=22),
                    ),
                    ft.Column([
                        ft.Text("StockAI PRO", size=24, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY),
                        market_status_text,
                    ], spacing=0),
                ], spacing=12),
                ft.Container(height=16),
                index_ticker_row,
                ft.Container(height=18),
                search_input,
                result_column,
                ft.Container(height=26),
                ft.Text("RECENT SEARCHES", size=12, weight=ft.FontWeight.W_700, color=TEXT_MUTED,
                        style=ft.TextStyle(letter_spacing=1.5)),
                ft.Container(height=6),
                recent_list,
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- NEWS SCREEN ----------
    news_list = ft.Column(spacing=10)

    def refresh_news_screen():
        news_list.controls.clear()
        items = get_news_items(db_conn)
        if not items:
            news_list.controls.append(
                ft.Text(
                    "No news yet. Tap 'Update Market Data' on Home to fetch today's top movers.",
                    color=TEXT_MUTED,
                )
            )
        else:
            for symbol, company_name, date, pct_change in items:
                up = pct_change >= 0
                color = GREEN if up else RED
                news_list.controls.append(
                    ft.Container(
                        padding=15,
                        border_radius=14,
                        bgcolor=SURFACE,
                        border=ft.border.all(1, BORDER),
                        content=ft.Column(
                            [
                                ft.Row(
                                    [
                                        ft.Column(
                                            [
                                                ft.Text(symbol, weight=ft.FontWeight.BOLD, size=15, color=TEXT_PRIMARY),
                                                ft.Text(company_name, size=11, color=TEXT_SECONDARY),
                                            ],
                                            spacing=0, expand=True,
                                        ),
                                        ft.Container(
                                            padding=ft.padding.symmetric(horizontal=8, vertical=3),
                                            border_radius=8, bgcolor=f"{color}22",
                                            content=ft.Text(f"{pct_change:+.2f}%", size=14, weight=ft.FontWeight.BOLD, color=color),
                                        ),
                                    ],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                ),
                                ft.Text(date, size=11, color=TEXT_MUTED),
                                ft.Row(
                                    [
                                        ft.TextButton(
                                            "News",
                                            icon=Icons.OPEN_IN_NEW,
                                            icon_color=ACCENT,
                                            style=ft.ButtonStyle(color=ACCENT),
                                            on_click=lambda e, c=company_name: page.launch_url(google_news_url(c)),
                                        ),
                                        ft.IconButton(
                                            Icons.COPY, icon_size=16, icon_color=TEXT_MUTED,
                                            tooltip="Copy",
                                            on_click=lambda e, s=symbol, p=pct_change: copy_to_clipboard(
                                                f"{s} {p:+.2f}%", "Stock"),
                                        ),
                                    ],
                                    alignment=ft.MainAxisAlignment.END,
                                ),
                            ],
                            spacing=2,
                        ),
                    )
                )
                news_list.controls.append(ft.Container(height=8))

    news_screen = ft.Container(
        padding=20,
        bgcolor=BG,
        content=ft.Column(
            [
                ft.Text("MARKET MOVERS - NEWS", size=20, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY,
                        style=ft.TextStyle(letter_spacing=1)),
                ft.Text("Auto-clears after 7 days", size=12, color=TEXT_MUTED),
                ft.Container(height=15),
                news_list,
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- WATCHLIST SCREEN ----------
    watchlist_list = ft.Column(spacing=0)
    watchlist_search_results = ft.Column(spacing=0)

    def on_add_stock_search(e):
        query = (add_watchlist_input.value or "").strip()
        watchlist_search_results.controls.clear()
        if len(query) >= 2:
            results = search_stock_db(db_conn, query)
            if results:
                for symbol, company_name, sector, price in results[:8]:
                    def make_add(sym, name):
                        def _add(e):
                            add_to_watchlist(db_conn, sym, name)
                            add_watchlist_input.value = ""
                            watchlist_search_results.controls.clear()
                            refresh_watchlist_list()
                            page.update()
                        return _add

                    watchlist_search_results.controls.append(
                        ft.Container(
                            bgcolor=SURFACE, border_radius=10, margin=ft.margin.only(bottom=4),
                            content=ft.ListTile(
                                leading=ft.Icon(Icons.ADD_CIRCLE_OUTLINE, color=ACCENT, size=20),
                                title=ft.Text(symbol, weight=ft.FontWeight.BOLD, size=14, color=TEXT_PRIMARY),
                                subtitle=ft.Text(company_name, size=12, color=TEXT_SECONDARY),
                                on_click=make_add(symbol, company_name),
                                dense=True,
                            ),
                        )
                    )
            else:
                watchlist_search_results.controls.append(
                    ft.Container(padding=10, content=ft.Text("No matches found", size=12, color=TEXT_MUTED))
                )
        page.update()

    add_watchlist_input = ft.TextField(
        hint_text="Type any 3 letters - covers all ~2000 NSE stocks",
        hint_style=ft.TextStyle(color=TEXT_MUTED, size=13),
        prefix_icon=Icons.SEARCH,
        border_radius=14,
        filled=True,
        fill_color=SURFACE_ALT,
        border_color=BORDER,
        focused_border_color=ACCENT,
        color=TEXT_PRIMARY,
        cursor_color=ACCENT,
        height=50,
        text_size=14,
        on_change=on_add_stock_search,
    )

    def refresh_watchlist_list():
        watchlist_list.controls.clear()
        favs = get_favorites_full(db_conn)
        if not favs:
            watchlist_list.controls.append(
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Icon(Icons.STAR_BORDER, size=54, color=TEXT_MUTED),
                            ft.Text("No stocks in your watchlist yet", color=TEXT_SECONDARY, weight=ft.FontWeight.W_600, size=14),
                            ft.Text("Search a stock above and tap it to add.", size=12, color=TEXT_MUTED),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=6,
                    ),
                    alignment=ft.alignment.center,
                    padding=36,
                )
            )
        else:
            AVATAR_COLORS = [ACCENT, GOLD, GREEN, "#AB47BC", "#26C6DA", "#EF5350"]
            for idx, (symbol, company_name, price, change_pct, last_updated) in enumerate(favs):

                def make_remove(sym):
                    def _remove(e):
                        remove_from_watchlist(db_conn, sym)
                        refresh_watchlist_list()
                        page.update()
                    return _remove

                if price and change_pct is not None:
                    up = change_pct >= 0
                    color = GREEN if up else RED
                    prev_price = price / (1 + change_pct / 100) if (100 + change_pct) != 0 else price
                    change_amount = price - prev_price
                    right_block = ft.Column(
                        [
                            ft.Text(f"{price:,.2f}", size=15, weight=ft.FontWeight.W_700, color=TEXT_PRIMARY),
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=6, vertical=2),
                                border_radius=6,
                                bgcolor=f"{color}22",
                                content=ft.Row(
                                    [
                                        ft.Icon(Icons.ARROW_UPWARD if up else Icons.ARROW_DOWNWARD, size=11, color=color),
                                        ft.Text(f"{change_amount:+,.2f} ({change_pct:+.2f}%)", size=11, color=color, weight=ft.FontWeight.W_700),
                                    ],
                                    spacing=2, tight=True,
                                ),
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.END, spacing=4, tight=True,
                    )
                else:
                    right_block = ft.Text("--", size=13, color=TEXT_MUTED)

                # TradingView-style compact premium card: avatar + symbol/name
                # on the left, price/change on the right. Tap opens full
                # details (copy + news live there).
                watchlist_list.controls.append(
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=12, vertical=12),
                        margin=ft.margin.only(bottom=8),
                        bgcolor=SURFACE,
                        border_radius=14,
                        border=ft.border.all(1, BORDER),
                        on_click=lambda e, s=symbol, c=company_name, p=price: show_stock_details(s, c, "N/A", p or 0),
                        content=ft.Row(
                            [
                                ft.CircleAvatar(
                                    content=ft.Text(symbol[0] if symbol else "?", size=14, weight=ft.FontWeight.BOLD, color=Colors.WHITE),
                                    radius=18,
                                    bgcolor=AVATAR_COLORS[idx % len(AVATAR_COLORS)],
                                ),
                                ft.Column(
                                    [
                                        ft.Text(symbol, weight=ft.FontWeight.BOLD, size=14, color=TEXT_PRIMARY,
                                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                        ft.Text(company_name, size=11, color=TEXT_SECONDARY,
                                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                    ],
                                    spacing=1, expand=True, tight=True,
                                ),
                                right_block,
                                ft.IconButton(Icons.CLOSE, icon_size=15, icon_color=TEXT_MUTED, on_click=make_remove(symbol)),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )

    watchlist_screen = ft.Container(
        padding=20,
        bgcolor=BG,
        content=ft.Column(
            [
                ft.Text("MY WATCHLIST", size=22, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY,
                        style=ft.TextStyle(letter_spacing=1)),
                ft.Text("Updates only when you tap 'Update Market Data' on Home", size=12, color=TEXT_MUTED),
                ft.Container(height=16),
                add_watchlist_input,
                watchlist_search_results,
                ft.Container(height=14),
                watchlist_list,
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- ANALYTICS SCREEN ----------
    def build_mover_row(rank, symbol, company_name, price, pct_change):
        up = pct_change >= 0
        color = GREEN if up else RED
        return ft.Container(
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            margin=ft.margin.only(bottom=6),
            bgcolor=SURFACE,
            border_radius=12,
            border=ft.border.all(1, BORDER),
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Text(str(rank), size=12, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                        bgcolor=SURFACE_ALT,
                        width=26, height=26, border_radius=13,
                        alignment=ft.alignment.center,
                        border=ft.border.all(1, BORDER),
                    ),
                    ft.Column(
                        [
                            ft.Text(symbol, weight=ft.FontWeight.BOLD, size=14, color=TEXT_PRIMARY),
                            ft.Text(company_name, size=11, color=TEXT_SECONDARY,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                        spacing=0, expand=True,
                    ),
                    ft.Column(
                        [
                            ft.Text(f"Rs.{price:,.2f}", size=13, weight=ft.FontWeight.W_700, color=TEXT_PRIMARY),
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=6, vertical=1),
                                border_radius=6,
                                bgcolor=f"{color}22",
                                content=ft.Row(
                                    [
                                        ft.Icon(Icons.ARROW_UPWARD if up else Icons.ARROW_DOWNWARD, size=11, color=color),
                                        ft.Text(f"{pct_change:+.2f}%", size=11, color=color, weight=ft.FontWeight.W_700),
                                    ],
                                    spacing=2, tight=True,
                                ),
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.END, spacing=3,
                    ),
                    ft.Column(
                        [
                            ft.IconButton(
                                Icons.OPEN_IN_NEW, icon_size=15, icon_color=ACCENT,
                                tooltip="News",
                                on_click=lambda e, c=company_name: page.launch_url(google_news_url(c)),
                            ),
                            ft.IconButton(
                                Icons.COPY, icon_size=15, icon_color=TEXT_MUTED,
                                tooltip="Copy",
                                on_click=lambda e, s=symbol, p=price, pc=pct_change: copy_to_clipboard(
                                    f"{s} Rs.{p:,.2f} ({pc:+.2f}%)", "Stock"),
                            ),
                        ],
                        spacing=0,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                spacing=6,
            ),
        )

    def build_mover_section(rows):
        body = ft.Column(spacing=0)
        if not rows:
            body.controls.append(
                ft.Text("No data yet. Tap 'Update Market Data' on Home.", color=TEXT_MUTED, size=12)
            )
        else:
            for row in rows:
                rank, symbol, company_name, price, pct_change = row
                body.controls.append(build_mover_row(rank, symbol, company_name, price, pct_change))
        return body

    # selected: None means neither list is shown (per your sketch: date + two
    # boxes, only the tapped one opens up). selected_date: None = latest.
    mover_state = {"selected": None, "selected_date": None}
    analytics_date_text = ft.Text("No data synced yet", size=13, color=TEXT_SECONDARY, weight=ft.FontWeight.W_600)
    analytics_list_body = ft.Column(spacing=0)
    analytics_date_dropdown = ft.Dropdown(
        label="Date", options=[], visible=False, width=170,
        border_radius=10, bgcolor=SURFACE_ALT, border_color=BORDER,
        focused_border_color=ACCENT, color=TEXT_PRIMARY, text_size=13,
        label_style=ft.TextStyle(color=TEXT_MUTED, size=12),
    )
    profit_growth_status_text = ft.Text("", size=11, color=TEXT_MUTED)

    def build_profit_growth_row(symbol, company_name, price, week52_high, pct_below_high):
        return ft.Container(
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            margin=ft.margin.only(bottom=6),
            bgcolor=SURFACE,
            border_radius=12,
            border=ft.border.all(1, BORDER),
            on_click=lambda e: show_stock_details(symbol, company_name, "N/A", price),
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(symbol, weight=ft.FontWeight.BOLD, size=14, color=TEXT_PRIMARY),
                            ft.Text(company_name, size=11, color=TEXT_SECONDARY,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                        spacing=0, expand=True,
                    ),
                    ft.Column(
                        [
                            ft.Text(f"Rs.{price:,.2f}", size=13, weight=ft.FontWeight.W_700, color=TEXT_PRIMARY),
                            ft.Text(f"52W High: Rs.{week52_high:,.2f}", size=10, color=TEXT_MUTED),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.END, spacing=2,
                    ),
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=8, vertical=4),
                        border_radius=8, bgcolor=f"{GOLD}22",
                        content=ft.Text(f"-{pct_below_high:.1f}%", size=12, color=GOLD, weight=ft.FontWeight.W_700),
                    ),
                    ft.IconButton(
                        Icons.OPEN_IN_NEW, icon_size=15, icon_color=ACCENT,
                        tooltip="News",
                        on_click=lambda e, c=company_name: page.launch_url(google_news_url(c)),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                spacing=6,
            ),
        )

    def render_mover_list():
        mtype = mover_state["selected"]
        analytics_list_body.controls.clear()

        if mtype is None:
            analytics_date_text.value = "Tap a button below to view"
            analytics_date_dropdown.visible = False
            analytics_list_body.controls.append(
                ft.Text("Nothing selected yet.", color=TEXT_MUTED, size=12)
            )
            return

        if mtype == "profit_growth":
            analytics_date_dropdown.visible = False
            last_scan = get_setting(db_conn, "profit_growth_last_scan_date")
            rows = get_profit_growth_stocks(db_conn)
            analytics_date_text.value = (
                f"Last scanned: {last_scan}" if last_scan else "Not scanned yet - runs after your next sync"
            )
            if not rows:
                analytics_list_body.controls.append(
                    ft.Text(
                        "No matches yet. This scan runs automatically (once/day) after "
                        "'Update Market Data' - it can take a while for ~2000 stocks.",
                        color=TEXT_MUTED, size=12,
                    )
                )
            else:
                for symbol, company_name, price, week52_high, pct_below_high, scan_date in rows:
                    analytics_list_body.controls.append(
                        build_profit_growth_row(symbol, company_name, price, week52_high, pct_below_high)
                    )
            return

        refresh_date_dropdown()
        chosen_date = mover_state["selected_date"]
        rows, data_date = get_market_movers(db_conn, mtype, chosen_date)
        analytics_date_text.value = f"Date: {data_date}" if data_date else "No data synced yet"
        analytics_list_body.controls.append(build_mover_section(rows))

    def refresh_date_dropdown():
        dates = get_available_mover_dates(db_conn)
        analytics_date_dropdown.options = [ft.dropdown.Option(d, d) for d in dates]
        if dates:
            analytics_date_dropdown.visible = True
            if mover_state["selected_date"] not in dates:
                mover_state["selected_date"] = dates[0]
            analytics_date_dropdown.value = mover_state["selected_date"]
        else:
            analytics_date_dropdown.visible = False

    def on_date_change(e):
        mover_state["selected_date"] = analytics_date_dropdown.value
        render_mover_list()
        page.update()

    analytics_date_dropdown.on_change = on_date_change

    def _pill_style(bgcolor, color):
        return ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=12),
            padding=14,
            elevation=0,
            text_style=ft.TextStyle(weight=ft.FontWeight.W_700, size=13),
        )

    def _reset_pills():
        gainer_btn.bgcolor = SURFACE_ALT
        gainer_btn.color = TEXT_SECONDARY
        loser_btn.bgcolor = SURFACE_ALT
        loser_btn.color = TEXT_SECONDARY
        profit_growth_btn.bgcolor = SURFACE_ALT
        profit_growth_btn.color = TEXT_SECONDARY

    def select_gainers(e):
        # Tap again to hide it (per your request: click to show, otherwise don't show)
        if mover_state["selected"] == "gainer":
            mover_state["selected"] = None
            _reset_pills()
        else:
            _reset_pills()
            mover_state["selected"] = "gainer"
            gainer_btn.bgcolor = GREEN
            gainer_btn.color = Colors.WHITE
        render_mover_list()
        page.update()

    def select_losers(e):
        if mover_state["selected"] == "loser":
            mover_state["selected"] = None
            _reset_pills()
        else:
            _reset_pills()
            mover_state["selected"] = "loser"
            loser_btn.bgcolor = RED
            loser_btn.color = Colors.WHITE
        render_mover_list()
        page.update()

    def select_profit_growth(e):
        if mover_state["selected"] == "profit_growth":
            mover_state["selected"] = None
            _reset_pills()
        else:
            _reset_pills()
            mover_state["selected"] = "profit_growth"
            profit_growth_btn.bgcolor = GOLD
            profit_growth_btn.color = Colors.BLACK
        render_mover_list()
        page.update()

    gainer_btn = ft.ElevatedButton(
        "Gainers", bgcolor=SURFACE_ALT, color=TEXT_SECONDARY,
        on_click=select_gainers, expand=True, style=_pill_style(SURFACE_ALT, TEXT_SECONDARY),
    )
    loser_btn = ft.ElevatedButton(
        "Losers", bgcolor=SURFACE_ALT, color=TEXT_SECONDARY,
        on_click=select_losers, expand=True, style=_pill_style(SURFACE_ALT, TEXT_SECONDARY),
    )
    profit_growth_btn = ft.ElevatedButton(
        "Profit Growth", bgcolor=SURFACE_ALT, color=TEXT_SECONDARY,
        on_click=select_profit_growth, expand=True, style=_pill_style(SURFACE_ALT, TEXT_SECONDARY),
    )

    def refresh_analytics_screen():
        refresh_date_dropdown()
        render_mover_list()

    analytics_screen = ft.Container(
        padding=20,
        bgcolor=BG,
        content=ft.Column(
            [
                ft.Text("ANALYTICS DASHBOARD", size=20, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY,
                        style=ft.TextStyle(letter_spacing=1)),
                ft.Container(height=14),
                ft.Container(
                    bgcolor=SURFACE, border_radius=16, border=ft.border.all(1, BORDER),
                    padding=16,
                    content=ft.Column(
                        [
                            ft.Row([analytics_date_text, analytics_date_dropdown],
                                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                            ft.Container(height=12),
                            ft.Row([gainer_btn, loser_btn, profit_growth_btn], spacing=8),
                            ft.Container(height=12),
                            analytics_list_body,
                        ]
                    ),
                ),
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- SETTINGS SCREEN ----------
    status_text = ft.Text("", color=TEXT_SECONDARY, size=12)

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
        if val == "light":
            page.theme_mode = ft.ThemeMode.LIGHT
        elif val == "dark":
            page.theme_mode = ft.ThemeMode.DARK
        else:
            page.theme_mode = ft.ThemeMode.SYSTEM
        page.update()

    def _input_style(**kwargs):
        base = dict(
            border_radius=10, filled=True, fill_color=SURFACE_ALT,
            border_color=BORDER, focused_border_color=ACCENT,
            color=TEXT_PRIMARY,
            label_style=ft.TextStyle(color=TEXT_MUTED),
        )
        base.update(kwargs)
        return base

    def _premium_button(text, icon=None, on_click=None, primary=True):
        return ft.ElevatedButton(
            text, icon=icon, on_click=on_click,
            color=Colors.WHITE if primary else TEXT_PRIMARY,
            bgcolor=ACCENT if primary else SURFACE_ALT,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=12), padding=14, elevation=0,
                text_style=ft.TextStyle(weight=ft.FontWeight.W_600, size=13),
            ),
        )

    def _section_card(title, subtitle, children):
        col = [ft.Text(title, size=15, weight=ft.FontWeight.W_700, color=TEXT_PRIMARY)]
        if subtitle:
            col.append(ft.Text(subtitle, size=12, color=TEXT_MUTED))
        col.append(ft.Container(height=10))
        col.extend(children)
        return ft.Container(
            bgcolor=SURFACE, border_radius=16, border=ft.border.all(1, BORDER),
            padding=18, margin=ft.margin.only(bottom=14),
            content=ft.Column(col, spacing=10),
        )

    theme_dropdown = ft.Dropdown(
        label="App Theme",
        value=stored_theme,
        options=[
            ft.dropdown.Option("system", "System Default"),
            ft.dropdown.Option("light", "Light"),
            ft.dropdown.Option("dark", "Dark"),
        ],
        on_change=on_theme_change,
        border_radius=10, bgcolor=SURFACE_ALT, border_color=BORDER,
        focused_border_color=ACCENT, color=TEXT_PRIMARY,
        label_style=ft.TextStyle(color=TEXT_MUTED),
    )

    def on_dhan_save(e):
        set_setting(db_conn, "dhan_client_id", (dhan_client_input.value or "").strip())
        set_setting(db_conn, "dhan_access_token", (dhan_token_input.value or "").strip())
        set_setting(db_conn, "dhan_scrip_fetched_date", "")  # force re-fetch of scrip master next sync
        dhan_status_text.value = "Dhan credentials saved."
        dhan_status_text.color = GREEN
        page.update()

    dhan_client_input = ft.TextField(
        label="Dhan Client ID",
        value=get_setting(db_conn, "dhan_client_id", ""),
        **_input_style(),
    )
    dhan_token_input = ft.TextField(
        label="Dhan Access Token",
        value=get_setting(db_conn, "dhan_access_token", ""),
        password=True,
        can_reveal_password=True,
        **_input_style(),
    )
    dhan_status_text = ft.Text("", size=12, color=TEXT_SECONDARY)

    # ---- Telegram auto-notifications ----
    telegram_phone_input = ft.TextField(
        label="Your Telegram Phone Number (for your reference)",
        value=get_setting(db_conn, "telegram_phone_number", ""),
        hint_text="+91XXXXXXXXXX",
        **_input_style(),
    )
    telegram_token_input = ft.TextField(
        label="Telegram Bot Token (from @BotFather)",
        value=get_setting(db_conn, "telegram_bot_token", ""),
        password=True,
        can_reveal_password=True,
        **_input_style(),
    )
    telegram_status_text = ft.Text(
        "Connected" if get_setting(db_conn, "telegram_chat_id") else "Not connected yet",
        size=12,
        color=GREEN if get_setting(db_conn, "telegram_chat_id") else TEXT_MUTED,
    )

    def on_telegram_save(e):
        set_setting(db_conn, "telegram_phone_number", (telegram_phone_input.value or "").strip())
        set_setting(db_conn, "telegram_bot_token", (telegram_token_input.value or "").strip())
        telegram_status_text.value = "Bot Token saved. Now send /start to your bot on Telegram, then tap 'Connect My Telegram'."
        telegram_status_text.color = ACCENT
        page.update()

    def on_telegram_connect(e):
        token = (telegram_token_input.value or "").strip()
        if not token:
            telegram_status_text.value = "Save your Bot Token first."
            telegram_status_text.color = RED
            page.update()
            return
        connect_btn.disabled = True
        connect_btn.text = "Connecting..."
        page.update()
        chat_id, msg = fetch_telegram_chat_id(token)
        if chat_id:
            set_setting(db_conn, "telegram_chat_id", chat_id)
            telegram_status_text.color = GREEN
        else:
            telegram_status_text.color = RED
        telegram_status_text.value = msg
        connect_btn.disabled = False
        connect_btn.text = "Connect My Telegram"
        page.update()

    connect_btn = _premium_button("Connect My Telegram", Icons.SEND, on_telegram_connect, primary=False)

    # ---- Full market universe / market-cap ranking controls ----
    universe_status_text = ft.Text("", size=12, color=TEXT_MUTED)

    def on_scan_universe(e):
        scan_universe_btn.disabled = True
        scan_universe_btn.text = "Scanning..."
        page.update()

        def progress_callback(msg):
            universe_status_text.value = msg
            page.update()

        def do_scan():
            universe = fetch_full_market_universe(db_conn, progress_callback)
            universe_status_text.value = f"Search index now covers {len(universe)} NSE stocks."
            universe_status_text.color = GREEN
            scan_universe_btn.disabled = False
            scan_universe_btn.text = "Scan Full Market (~2000 stocks)"
            page.update()

        threading.Thread(target=do_scan, daemon=True).start()

    scan_universe_btn = _premium_button("Scan Full Market (~2000 stocks)", Icons.TRAVEL_EXPLORE, on_scan_universe, primary=False)

    market_cap_status_text = ft.Text(
        f"Last updated: {get_setting(db_conn, 'market_cap_last_updated', 'never')}",
        size=12, color=TEXT_MUTED,
    )

    def on_fetch_market_caps(e):
        cap_btn.disabled = True
        cap_btn.text = "Ranking by market cap..."
        page.update()

        def progress_callback(msg):
            market_cap_status_text.value = msg
            page.update()

        def do_fetch():
            fetch_market_caps(db_conn, progress_callback)
            cap_btn.disabled = False
            cap_btn.text = "Update Market-Cap Ranking (Slow)"
            page.update()

        threading.Thread(target=do_fetch, daemon=True).start()

    cap_btn = _premium_button("Update Market-Cap Ranking (Slow)", Icons.LEADERBOARD, on_fetch_market_caps, primary=False)

    # ---- Profit Growth screener manual trigger ----
   def on_scan_profit_growth(e):
        pg_scan_btn.disabled = True
        pg_scan_btn.text = "Scanning..."
        page.update()

        def progress_callback(msg):
            profit_growth_status_text.value = msg
            page.update()

        def do_scan():
            scan_profit_growth_universe(db_conn, progress_callback)
            pg_scan_btn.disabled = False
            pg_scan_btn.text = "Scan Now (Profit Growth)"
            refresh_analytics_screen()
            page.update()

        threading.Thread(target=do_scan, daemon=True).start()

    pg_scan_btn = _premium_button("Scan Now (Profit Growth)", Icons.QUERY_STATS, on_scan_profit_growth, primary=False)

    settings_screen = ft.Container(
        padding=20,
        bgcolor=BG,
        content=ft.Column(
            [
                ft.Text("SETTINGS", size=22, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY,
                        style=ft.TextStyle(letter_spacing=1)),
                ft.Container(height=16),

                _section_card("Appearance", None, [theme_dropdown]),

                _section_card(
                    "Stock Search Coverage", None,
                    [
                        ft.Text(
                            "Runs automatically on every 'Update Market Data' - use this only if "
                            "you want to refresh the search index without a full sync.",
                            size=12, color=TEXT_MUTED,
                        ),
                        scan_universe_btn, universe_status_text,
                        ft.Divider(color=BORDER, height=20),
                        ft.Text(
                            "Ranks search & analytics by market capitalisation. Scans stocks one "
                            "by one (no free bulk source exists) so it can take 15-30+ minutes - "
                            "run occasionally, not every day.",
                            size=12, color=TEXT_MUTED,
                        ),
                        cap_btn, market_cap_status_text,
                    ],
                ),

                _section_card(
                    "Profit Growth Screener", "8 straight quarters of rising profit + 15%+ below 52-week high",
                    [
                        ft.Text(
                            "Runs automatically once/day after 'Update Market Data' (scans the "
                            "full ~2000 stock universe, so it takes a while). Use this button to "
                            "run it right now instead of waiting.",
                            size=12, color=TEXT_MUTED,
                        ),
                        pg_scan_btn, profit_growth_status_text,
                    ],
                ),

                _section_card(
                    "Dhan API", "Fastest, most reliable data source",
                    [
                        ft.Text(
                            "Free: open a Dhan account, generate an Access Token from web.dhan.co. "
                            "Leave blank to use free public sources instead (slower). "
                            "Fallback order: Dhan -> NSE live -> Yahoo Finance full scan.",
                            size=12, color=TEXT_MUTED,
                        ),
                        dhan_client_input, dhan_token_input,
                        _premium_button("Save Dhan Credentials", None, on_dhan_save),
                        dhan_status_text,
                    ],
                ),

                _section_card(
                    "Telegram Auto-Updates", "Gainers/losers (text + PDF) sent automatically every sync",
                    [
                        ft.Text(
                            "One-time setup:\n"
                            "1. Message @BotFather on Telegram -> /newbot -> copy the Bot Token.\n"
                            "2. Send any message (e.g. /start) to your new bot.\n"
                            "3. Tap 'Connect My Telegram' - no manual step needed again after that.",
                            size=12, color=TEXT_MUTED,
                        ),
                        telegram_phone_input, telegram_token_input,
                        ft.Row([_premium_button("Save Bot Token", None, on_telegram_save), connect_btn], spacing=10),
                        telegram_status_text,
                    ],
                ),

                _section_card(
                    "Database Management", None,
                    [
                        ft.Container(
                            bgcolor=SURFACE_ALT, border_radius=10,
                            content=ft.ListTile(
                                leading=ft.Icon(Icons.BACKUP, color=ACCENT),
                                title=ft.Text("Backup Database", color=TEXT_PRIMARY, size=14),
                                on_click=on_backup,
                            ),
                        ),
                        ft.Container(
                            bgcolor=SURFACE_ALT, border_radius=10,
                            content=ft.ListTile(
                                leading=ft.Icon(Icons.DELETE_FOREVER, color=RED),
                                title=ft.Text("Clear Search History", color=TEXT_PRIMARY, size=14),
                                on_click=on_clear,
                            ),
                        ),
                        status_text,
                    ],
                ),

                _section_card(
                    "App Info", None,
                    [
                        ft.Text("Version: 2.0.0 (StockAI Pro)", size=12, color=TEXT_SECONDARY),
                        ft.Text("Database: Local SQLite", size=12, color=TEXT_SECONDARY),
                        ft.Text("Data source order: Dhan API -> NSE live -> Yahoo Finance (fallback)", size=12, color=TEXT_SECONDARY),
                        ft.Text("Search coverage: full NSE universe (~2000 stocks)", size=12, color=TEXT_SECONDARY),
                    ],
                ),
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- BOTTOM NAVIGATION ----------
    screens = [home_screen, news_screen, watchlist_screen, analytics_screen, settings_screen]

    def change_tab(e):
        idx = e.control.selected_index
        if idx == 1:
            refresh_news_screen()
        elif idx == 2:
            refresh_watchlist_list()
        elif idx == 3:
            refresh_analytics_screen()
        main_content.content = screens[idx]
        page.update()

    bottom_nav = ft.NavigationBar(
        selected_index=0,
        on_change=change_tab,
        bgcolor=SURFACE,
        indicator_color=ACCENT_SOFT,
        destinations=[
            ft.NavigationBarDestination(icon=Icons.HOME_OUTLINED, selected_icon=Icons.HOME, label="Home"),
            ft.NavigationBarDestination(icon=Icons.ARTICLE_OUTLINED, selected_icon=Icons.ARTICLE, label="News"),
            ft.NavigationBarDestination(icon=Icons.STAR_BORDER, selected_icon=Icons.STAR, label="Watchlist"),
            ft.NavigationBarDestination(icon=Icons.ANALYTICS_OUTLINED, selected_icon=Icons.ANALYTICS, label="Analytics"),
            ft.NavigationBarDestination(icon=Icons.SETTINGS_OUTLINED, selected_icon=Icons.SETTINGS, label="Settings"),
        ],
    )

    # ---------- PASSWORD LOCK SCREEN ----------
    APP_PASSWORD = "8707352902"

    password_input = ft.TextField(
        hint_text="Enter Password",
        hint_style=ft.TextStyle(color=TEXT_MUTED),
        password=True,
        can_reveal_password=True,
        border_radius=14,
        filled=True,
        fill_color=SURFACE_ALT,
        border_color=BORDER,
        focused_border_color=ACCENT,
        color=TEXT_PRIMARY,
        cursor_color=ACCENT,
        height=55,
        text_align=ft.TextAlign.CENTER,
        width=260,
    )
    password_error = ft.Text("", color=RED, size=13)

    def load_home():
        try:
            time.sleep(2)
            # Populate the full ~2000 stock search index right away, so
            # search/watchlist-add works even before the first refresh cycle.
            def universe_progress(msg):
                market_status_text.value = msg
                page.update()
            threading.Thread(
                target=lambda: fetch_full_market_universe(db_conn, universe_progress),
                daemon=True,
            ).start()

            refresh_recent_list()
            refresh_watchlist_list()
            refresh_news_screen()
            refresh_analytics_screen()
            main_content.content = home_screen
            page.navigation_bar = bottom_nav
            page.update()
            # Live data starts flowing only now that the app is actually open,
            # and stops automatically via page.on_disconnect when it's closed.
            threading.Thread(target=auto_refresh_loop, daemon=True).start()
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
        expand=True,
        bgcolor=BG,
        alignment=ft.alignment.center,
        content=ft.Column(
            [
                ft.Container(
                    content=ft.Icon(Icons.LOCK, size=54, color=ACCENT),
                    padding=20, bgcolor=SURFACE, border_radius=20,
                    border=ft.border.all(1, BORDER),
                ),
                ft.Container(height=18),
                ft.Text("StockAI PRO", size=26, weight=ft.FontWeight.W_900, color=TEXT_PRIMARY,
                        style=ft.TextStyle(letter_spacing=1.5)),
                ft.Text("Enter password to continue", size=13, color=TEXT_SECONDARY),
                ft.Container(height=24),
                password_input,
                ft.Container(height=12),
                ft.ElevatedButton(
                    "OK", on_click=check_password, width=260,
                    color=Colors.WHITE, bgcolor=ACCENT,
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=14), padding=16, elevation=0),
                ),
                ft.Container(height=8),
                password_error,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        ),
    )

    # ---------- INITIAL RENDER: PASSWORD FIRST ----------
    main_content.content = password_screen
    page.add(main_content)


if __name__ == "__main__":
    ft.app(target=main) 
