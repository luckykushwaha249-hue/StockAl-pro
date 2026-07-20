import flet as ft
import sqlite3
import yfinance as yf
import requests
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
# FALLBACK STOCK LIST
# Used only if the live Nifty 500 fetch (and the cache) both fail,
# e.g. on the very first run with no internet. This keeps the app
# usable even when the full list can't be downloaded.
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

    cursor.execute('''CREATE TABLE IF NOT EXISTS nifty500_cache (
                        symbol TEXT PRIMARY KEY,
                        company_name TEXT)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS dhan_scrip_cache (
                        symbol TEXT PRIMARY KEY,
                        security_id TEXT)''')

    # Safe migration: add new columns to favorites if they don't exist yet
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
# NIFTY 500 UNIVERSE (fetched live, cached, with fallback)
# ==========================================
def fetch_nifty500_universe(conn):
    """
    Tries to download the official Nifty 500 constituent list from NSE.
    On success, caches it to the database and updates stock_master.
    On failure, uses whatever was cached from a previous successful run.
    If there is no cache either (e.g. very first run, no internet),
    falls back to a smaller fixed list of well-known large-cap stocks.
    Returns a list of (symbol, company_name) tuples.
    """
    cursor = conn.cursor()
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/csv,application/csv,*/*",
        }
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        resp = session.get(
            "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
            headers=headers, timeout=15,
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
                "INSERT OR REPLACE INTO nifty500_cache (symbol, company_name) VALUES (?, ?)",
                symbols,
            )
            cursor.executemany(
                "INSERT OR IGNORE INTO stock_master (symbol, company_name, sector, price) VALUES (?, ?, 'N/A', 0.0)",
                symbols,
            )
            conn.commit()
            return symbols
    except Exception:
        pass

    # Fall back to cache from a previous successful fetch
    cursor.execute("SELECT symbol, company_name FROM nifty500_cache")
    cached = cursor.fetchall()
    if cached:
        return cached

    # Last resort: small fixed list
    return FALLBACK_UNIVERSE


def search_stock_db(conn, query):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT symbol, company_name, sector, price FROM stock_master "
        "WHERE symbol LIKE ? OR company_name LIKE ? LIMIT 30",
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
    Uses the authenticated DhanHQ market data API (requires a free Dhan
    account + API access token, set in Settings) to fetch OHLC data for
    the whole stock universe and compute Top 10 Gainers/Losers. This is
    far more reliable than scraping a website since it's an official,
    authenticated API. Returns (gainers, losers, data_date) or
    (None, None, None) if Dhan isn't configured or the call fails.
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

        universe = fetch_nifty500_universe(conn)
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
    Fetches pre-computed top gainers/losers directly from NSE's live
    market-movers endpoint (the same data source behind nseindia.com and
    Groww's 'Top Gainers/Losers' pages). Much faster than downloading
    all 500 stocks ourselves. Returns (gainers, losers, data_date) or
    (None, None, None) if it couldn't be fetched/parsed.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        }
        session = requests.Session()
        session.get("https://www.nseindia.com/market-data/top-gainers-losers", headers=headers, timeout=10)

        resp_g = session.get(
            "https://www.nseindia.com/api/live-analysis-variations?index=gainers",
            headers=headers, timeout=15,
        )
        resp_g.raise_for_status()
        data_g = resp_g.json()

        resp_l = session.get(
            "https://www.nseindia.com/api/live-analysis-variations?index=loosers",
            headers=headers, timeout=15,
        )
        resp_l.raise_for_status()
        data_l = resp_l.json()

        def extract(payload):
            # "allSec" = All Securities on NSE, the broadest live-movers list NSE
            # publishes (their site doesn't expose a Nifty-500-only cut of this
            # particular feed, so this is the closest official equivalent).
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
    Refreshes Top 10 Gainers / Top 10 Losers, the watchlist, and the news
    feed. Tries the fast NSE live-movers endpoint first (a few seconds);
    if that's unavailable, falls back to scanning the whole stock
    universe ourselves (slower, a minute or more).
    """
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, company_name FROM stock_master")
        name_lookup = {row[0]: row[1] for row in cursor.fetchall()}

        if progress_callback:
            progress_callback("Trying Dhan API...")
        gainers, losers, data_date = fetch_top_movers_from_dhan(conn, name_lookup)
        used_fallback = False
        source_used = "Dhan"

        if not gainers or not losers:
            if progress_callback:
                progress_callback("Trying NSE live data...")
            gainers, losers, data_date = fetch_top_movers_from_nse(name_lookup)
            source_used = "NSE"

        if not gainers or not losers:
            used_fallback = True
            source_used = "Full Scan"
            if progress_callback:
                progress_callback("Fast methods unavailable, doing a full scan instead (this is slower)...")
            gainers, losers, data_date = _scan_full_universe_for_movers(conn, progress_callback)

        if not gainers or not losers or not data_date:
            return "Sync Failed: No data received (check internet connection)", False

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


def _scan_full_universe_for_movers(conn, progress_callback=None):
    """
    Fallback used only if the fast NSE endpoint is unreachable: downloads
    price data for the whole stock universe ourselves and computes top
    gainers/losers manually. Slower (can take a minute or more).
    """
    universe = fetch_nifty500_universe(conn)
    symbols_only = [s for s, _ in universe]
    name_lookup = {s: n for s, n in universe}
    tickers = [f"{s}.NS" for s in symbols_only]

    if progress_callback:
        progress_callback(f"Downloading price data for {len(tickers)} stocks (this can take a minute)...")

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
# 2. MAIN APPLICATION
# ==========================================
def main(page: ft.Page):
    page.title = "StockAI Pro"
    page.padding = 0

    db_conn = init_db()

    stored_theme = get_setting(db_conn, "theme_mode", "system")
    if stored_theme == "light":
        page.theme_mode = ft.ThemeMode.LIGHT
    elif stored_theme == "dark":
        page.theme_mode = ft.ThemeMode.DARK
    else:
        page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme_seed="blue", use_material3=True)

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
                    "Read News on Google",
                    icon=Icons.OPEN_IN_NEW,
                    on_click=lambda e: page.launch_url(google_news_url(company_name)),
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
        if query:
            run_search(query)

    search_input.on_submit = handle_search

    sync_status_text = ft.Text(get_last_sync_display(db_conn), size=12, color=Colors.GREY)

    def on_update_market_data(e):
        update_button.disabled = True
        update_button.text = "Updating..."
        sync_status_text.value = "Starting sync..."
        sync_status_text.color = Colors.GREY
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

    update_button = ft.ElevatedButton(
        "Update Market Data",
        icon=Icons.REFRESH,
        on_click=on_update_market_data,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=15), padding=15),
    )

    home_screen = ft.Container(
        expand=True,
        padding=20,
        content=ft.Column(
            [
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
                    color=Colors.GREY_500,
                )
            )
        else:
            for symbol, company_name, date, pct_change in items:
                up = pct_change >= 0
                news_list.controls.append(
                    ft.Card(
                        content=ft.Container(
                            padding=15,
                            content=ft.Column(
                                [
                                    ft.Row([
                                        ft.Icon(Icons.TRENDING_UP if up else Icons.TRENDING_DOWN,
                                                color=Colors.GREEN if up else Colors.RED),
                                        ft.Text(
                                            f"{symbol} {'gained' if up else 'fell'} {abs(pct_change):.2f}% today",
                                            weight=ft.FontWeight.BOLD, size=15, expand=True,
                                        ),
                                    ]),
                                    ft.Text(f"{company_name} - {date}", size=12, color=Colors.GREY_500),
                                    ft.TextButton(
                                        "Read on Google News",
                                        icon=Icons.OPEN_IN_NEW,
                                        on_click=lambda e, c=company_name: page.launch_url(google_news_url(c)),
                                    ),
                                ],
                                spacing=4,
                            ),
                        )
                    )
                )

    news_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Market Movers - News", size=24, weight=ft.FontWeight.BOLD),
                ft.Text("Auto-clears after 7 days", size=12, color=Colors.GREY_500),
                ft.Container(height=15),
                news_list,
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- WATCHLIST SCREEN ----------
    watchlist_list = ft.Column(spacing=8)
    add_watchlist_input = ft.TextField(
        hint_text="Type stock symbol (e.g. RELIANCE)",
        border_radius=25,
        filled=True,
        height=48,
        text_size=14,
        expand=True,
    )

    def on_add_watchlist(e):
        symbol = (add_watchlist_input.value or "").strip().upper()
        if not symbol:
            return
        cursor = db_conn.cursor()
        cursor.execute("SELECT company_name FROM stock_master WHERE symbol=?", (symbol,))
        row = cursor.fetchone()
        company_name = row[0] if row else symbol
        add_to_watchlist(db_conn, symbol, company_name)
        add_watchlist_input.value = ""
        refresh_watchlist_list()
        page.update()

    def refresh_watchlist_list():
        watchlist_list.controls.clear()
        favs = get_favorites_full(db_conn)
        if not favs:
            watchlist_list.controls.append(
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Icon(Icons.STAR_BORDER, size=60, color=Colors.GREY_300),
                            ft.Text("No stocks in your watchlist yet", color=Colors.GREY_500, weight=ft.FontWeight.W_600),
                            ft.Text("Type a symbol above and tap Add.", size=12, color=Colors.GREY_500),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    alignment=ft.alignment.center,
                    padding=30,
                )
            )
        else:
            for symbol, company_name, price, change_pct, last_updated in favs:
                up = (change_pct or 0) >= 0
                pct_text = f"{change_pct:+.2f}%" if change_pct is not None else "Not synced yet"
                price_text = f"Rs.{price:,.2f}" if price else "Not synced yet"

                def make_remove(sym):
                    def _remove(e):
                        remove_from_watchlist(db_conn, sym)
                        refresh_watchlist_list()
                        page.update()
                    return _remove

                watchlist_list.controls.append(
                    ft.Card(
                        content=ft.ListTile(
                            leading=ft.Icon(Icons.SHOW_CHART),
                            title=ft.Text(f"{symbol} - {company_name}", weight=ft.FontWeight.BOLD),
                            subtitle=ft.Text(
                                f"{price_text}" + (f" - {last_updated}" if last_updated else "")
                            ),
                            trailing=ft.Row(
                                [
                                    ft.Icon(
                                        Icons.ARROW_UPWARD if up else Icons.ARROW_DOWNWARD,
                                        color=Colors.GREEN if up else Colors.RED, size=16,
                                    ) if change_pct is not None else ft.Container(),
                                    ft.Text(
                                        pct_text,
                                        color=Colors.GREEN if up else Colors.RED if change_pct is not None else Colors.GREY_500,
                                        weight=ft.FontWeight.W_600,
                                    ),
                                    ft.IconButton(Icons.CLOSE, icon_size=16, on_click=make_remove(symbol)),
                                ],
                                tight=True,
                            ),
                        )
                    )
                )

    watchlist_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("My Watchlist", size=24, weight=ft.FontWeight.BOLD),
                ft.Text("Updates only when you tap 'Update Market Data' on Home", size=12, color=Colors.GREY_500),
                ft.Container(height=15),
                ft.Row([add_watchlist_input, ft.IconButton(Icons.ADD_CIRCLE, icon_color=Colors.BLUE_700, on_click=on_add_watchlist)]),
                ft.Container(height=15),
                watchlist_list,
            ],
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    # ---------- ANALYTICS SCREEN ----------
    def build_mover_row(rank, symbol, company_name, price, pct_change):
        up = pct_change >= 0
        return ft.Container(
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Text(str(rank), size=12, weight=ft.FontWeight.BOLD, color=Colors.WHITE),
                        bgcolor=Colors.BLUE_GREY_400,
                        width=24, height=24, border_radius=12,
                        alignment=ft.alignment.center,
                    ),
                    ft.Column(
                        [
                            ft.Text(symbol, weight=ft.FontWeight.BOLD, size=14),
                            ft.Text(company_name, size=11, color=Colors.GREY_500),
                        ],
                        spacing=0, expand=True,
                    ),
                    ft.Column(
                        [
                            ft.Text(f"Rs.{price:,.2f}", size=13, weight=ft.FontWeight.W_600),
                            ft.Row(
                                [
                                    ft.Icon(
                                        Icons.ARROW_UPWARD if up else Icons.ARROW_DOWNWARD,
                                        size=12, color=Colors.GREEN if up else Colors.RED,
                                    ),
                                    ft.Text(f"{pct_change:+.2f}%", size=12,
                                            color=Colors.GREEN if up else Colors.RED,
                                            weight=ft.FontWeight.W_600),
                                ],
                                spacing=2,
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.END, spacing=2,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                spacing=10,
            ),
        )

    def build_mover_section(title, icon, accent_color, rows):
        body = ft.Column(spacing=2)
        if not rows:
            body.controls.append(
                ft.Text("No data yet. Tap 'Update Market Data' on Home.", color=Colors.GREY_500, size=12)
            )
        else:
            for i, row in enumerate(rows):
                rank, symbol, company_name, price, pct_change = row
                body.controls.append(build_mover_row(rank, symbol, company_name, price, pct_change))
                if i < len(rows) - 1:
                    body.controls.append(ft.Divider(height=1))
        return ft.Card(
            content=ft.Container(
                padding=15,
                content=ft.Column(
                    [
                        ft.Row([ft.Icon(icon, color=accent_color), ft.Text(title, size=17, weight=ft.FontWeight.BOLD, color=accent_color)]),
                        ft.Divider(),
                        body,
                    ]
                ),
            )
        )

    analytics_date_text = ft.Text("No data synced yet", size=13, color=Colors.GREY_500)
    analytics_content = ft.Column(spacing=15)

    def refresh_analytics_screen():
        gainers, date1 = get_market_movers(db_conn, "gainer")
        losers, date2 = get_market_movers(db_conn, "loser")
        data_date = date1 or date2
        analytics_date_text.value = f"Data as of: {data_date} (Nifty 500)" if data_date else "No data synced yet"

        analytics_content.controls.clear()
        analytics_content.controls.append(
            build_mover_section("Top 10 Gainers", Icons.TRENDING_UP, Colors.GREEN, gainers)
        )
        analytics_content.controls.append(
            build_mover_section("Top 10 Losers", Icons.TRENDING_DOWN, Colors.RED, losers)
        )

    analytics_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Analytics Dashboard", size=24, weight=ft.FontWeight.BOLD),
                analytics_date_text,
                ft.Container(height=10),
                analytics_content,
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

    theme_dropdown = ft.Dropdown(
        label="App Theme",
        value=stored_theme,
        options=[
            ft.dropdown.Option("system", "System Default"),
            ft.dropdown.Option("light", "Light"),
            ft.dropdown.Option("dark", "Dark"),
        ],
        on_change=on_theme_change,
    )

    def on_dhan_save(e):
        set_setting(db_conn, "dhan_client_id", (dhan_client_input.value or "").strip())
        set_setting(db_conn, "dhan_access_token", (dhan_token_input.value or "").strip())
        set_setting(db_conn, "dhan_scrip_fetched_date", "")  # force re-fetch of scrip master next sync
        dhan_status_text.value = "Dhan credentials saved."
        dhan_status_text.color = Colors.GREEN
        page.update()

    dhan_client_input = ft.TextField(
        label="Dhan Client ID",
        value=get_setting(db_conn, "dhan_client_id", ""),
        border_radius=10,
    )
    dhan_token_input = ft.TextField(
        label="Dhan Access Token",
        value=get_setting(db_conn, "dhan_access_token", ""),
        password=True,
        can_reveal_password=True,
        border_radius=10,
    )
    dhan_status_text = ft.Text("", size=12)

    settings_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Settings", size=28, weight=ft.FontWeight.BOLD),
                ft.Container(height=15),
                ft.Text("Appearance", size=18, weight=ft.FontWeight.W_600),
                theme_dropdown,
                ft.Container(height=20),
                ft.Text("Dhan API (fastest, most reliable data source)", size=18, weight=ft.FontWeight.W_600),
                ft.Text(
                    "Free to get: open a Dhan account, then generate an Access Token from web.dhan.co. "
                    "Leave blank to use free public data sources instead (slower).",
                    size=12, color=Colors.GREY_500,
                ),
                dhan_client_input,
                dhan_token_input,
                ft.ElevatedButton("Save Dhan Credentials", on_click=on_dhan_save),
                dhan_status_text,
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
                    ft.Text("Version: 1.0.0 (StockAI Pro)"),
                    ft.Text("Database: Local SQLite"),
                    ft.Text("Data source: Dhan API / NSE live data / Yahoo Finance (fallback order)"),
                ]))),
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
        password=True,
        can_reveal_password=True,
        border_radius=30,
        filled=True,
        border_color=Colors.TRANSPARENT,
        height=55,
        text_align=ft.TextAlign.CENTER,
        width=260,
    )
    password_error = ft.Text("", color=Colors.RED, size=13)

    def background_auto_sync():
        try:
            last_sync = get_setting(db_conn, "last_auto_sync_date")
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
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
        expand=True,
        alignment=ft.alignment.center,
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
