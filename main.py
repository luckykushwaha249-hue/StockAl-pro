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

    # ---------- CLIPBOARD COPY HELPER ----------
    def copy_to_clipboard(text, label="Text"):
        page.set_clipboard(text)
        page.snack_bar = ft.SnackBar(content=ft.Text(f"{label} copied to clipboard"), duration=1200)
        page.snack_bar.open = True
        page.update()

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

        def on_copy_click(e):
            copy_to_clipboard(f"{symbol} - {company_name} - Rs.{price:,.2f}", "Stock info")

        details_page = ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.IconButton(Icons.ARROW_BACK, on_click=go_back),
                    ft.Text(f"{symbol} Details", size=24, weight=ft.FontWeight.BOLD),
                    ft.Row([
                        ft.IconButton(Icons.COPY, on_click=on_copy_click, tooltip="Copy stock info"),
                        ft.IconButton(content=fav_icon, on_click=on_fav_click, tooltip="Add/Remove Watchlist"),
                    ], spacing=0),
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
        hint_text="Search Stock (e.g. RELIANCE, TCS, RELAXO)",
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
            # Real-time refresh: EVERY screen's data reflects the new sync immediately
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
                color = Colors.GREEN if up else Colors.RED
                news_list.controls.append(
                    ft.Container(
                        padding=15,
                        border_radius=12,
                        border=ft.border.all(1, Colors.GREY_300),
                        content=ft.Column(
                            [
                                ft.Row(
                                    [
                                        ft.Column(
                                            [
                                                ft.Text(symbol, weight=ft.FontWeight.BOLD, size=15),
                                                ft.Text(company_name, size=11, color=Colors.GREY_500),
                                            ],
                                            spacing=0, expand=True,
                                        ),
                                        ft.Text(f"{pct_change:+.2f}%", size=16, weight=ft.FontWeight.BOLD, color=color),
                                    ],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                ),
                                ft.Text(date, size=11, color=Colors.GREY_400),
                                ft.Row(
                                    [
                                        ft.TextButton(
                                            "News",
                                            icon=Icons.OPEN_IN_NEW,
                                            icon_color=Colors.BLUE_700,
                                            on_click=lambda e, c=company_name: page.launch_url(google_news_url(c)),
                                        ),
                                        ft.IconButton(
                                            Icons.COPY, icon_size=16,
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
                        ft.ListTile(
                            leading=ft.Icon(Icons.ADD_CIRCLE_OUTLINE, color=Colors.BLUE_700),
                            title=ft.Text(symbol, weight=ft.FontWeight.BOLD, size=14),
                            subtitle=ft.Text(company_name, size=12),
                            on_click=make_add(symbol, company_name),
                            dense=True,
                        )
                    )
            else:
                watchlist_search_results.controls.append(
                    ft.Container(padding=10, content=ft.Text("No matches found", size=12, color=Colors.GREY_500))
                )
        page.update()

    add_watchlist_input = ft.TextField(
        hint_text="Type any 3 letters - covers all ~2000 NSE stocks",
        prefix_icon=Icons.SEARCH,
        border_radius=25,
        filled=True,
        height=48,
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
                            ft.Icon(Icons.STAR_BORDER, size=60, color=Colors.GREY_300),
                            ft.Text("No stocks in your watchlist yet", color=Colors.GREY_500, weight=ft.FontWeight.W_600),
                            ft.Text("Search a stock above and tap it to add.", size=12, color=Colors.GREY_500),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    alignment=ft.alignment.center,
                    padding=30,
                )
            )
        else:
            for symbol, company_name, price, change_pct, last_updated in favs:

                def make_remove(sym):
                    def _remove(e):
                        remove_from_watchlist(db_conn, sym)
                        refresh_watchlist_list()
                        page.update()
                    return _remove

                if price and change_pct is not None:
                    up = change_pct >= 0
                    color = Colors.GREEN if up else Colors.RED
                    prev_price = price / (1 + change_pct / 100) if (100 + change_pct) != 0 else price
                    change_amount = price - prev_price
                    right_block = ft.Column(
                        [
                            ft.Text(f"Rs.{price:,.2f}", size=15, weight=ft.FontWeight.BOLD),
                            ft.Row(
                                [
                                    ft.Icon(Icons.ARROW_UPWARD if up else Icons.ARROW_DOWNWARD, size=12, color=color),
                                    ft.Text(f"{change_amount:+,.2f} ({change_pct:+.2f}%)", size=12, color=color, weight=ft.FontWeight.W_600),
                                ],
                                spacing=2,
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.END, spacing=2,
                    )
                else:
                    right_block = ft.Text("Not synced yet", size=12, color=Colors.GREY_500)

                watchlist_list.controls.append(
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=6, vertical=12),
                        content=ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text(symbol, weight=ft.FontWeight.BOLD, size=15),
                                        ft.Text(company_name, size=11, color=Colors.GREY_500),
                                    ],
                                    spacing=0, expand=True,
                                ),
                                right_block,
                                ft.IconButton(
                                    Icons.COPY, icon_size=16,
                                    tooltip="Copy",
                                    on_click=lambda e, s=symbol, p=price: copy_to_clipboard(
                                        f"{s} - Rs.{p:,.2f}" if p else s, "Stock"),
                                ),
                                ft.IconButton(
                                    Icons.OPEN_IN_NEW, icon_size=16,
                                    tooltip="News",
                                    on_click=lambda e, c=company_name: page.launch_url(google_news_url(c)),
                                ),
                                ft.IconButton(Icons.CLOSE, icon_size=16, on_click=make_remove(symbol)),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                    )
                )
                watchlist_list.controls.append(ft.Divider(height=1))

    watchlist_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("My Watchlist", size=24, weight=ft.FontWeight.BOLD),
                ft.Text("Updates only when you tap 'Update Market Data' on Home", size=12, color=Colors.GREY_500),
                ft.Container(height=15),
                add_watchlist_input,
                watchlist_search_results,
                ft.Container(height=15),
                ft.Card(content=ft.Container(padding=10, content=watchlist_list)),
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
                    ft.Column(
                        [
                            ft.IconButton(
                                Icons.OPEN_IN_NEW, icon_size=15,
                                tooltip="News",
                                on_click=lambda e, c=company_name: page.launch_url(google_news_url(c)),
                            ),
                            ft.IconButton(
                                Icons.COPY, icon_size=15,
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
        return body

    # selected: None means neither list is shown (per your sketch: date + two
    # boxes, only the tapped one opens up). selected_date: None = latest.
    mover_state = {"selected": None, "selected_date": None}
    analytics_date_text = ft.Text("No data synced yet", size=13, color=Colors.GREY_500)
    analytics_list_body = ft.Column(spacing=2)
    analytics_date_dropdown = ft.Dropdown(label="Date", options=[], visible=False, width=180)

    def render_mover_list():
        mtype = mover_state["selected"]
        analytics_list_body.controls.clear()

        if mtype is None:
            analytics_date_text.value = "Tap 'Top 10 Gainers' or 'Top 10 Losers' to view"
            analytics_list_body.controls.append(
                ft.Text("Nothing selected yet.", color=Colors.GREY_500, size=12)
            )
            return

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

    def select_gainers(e):
        # Tap again to hide it (per your request: click to show, otherwise don't show)
        if mover_state["selected"] == "gainer":
            mover_state["selected"] = None
            gainer_btn.bgcolor = Colors.GREY_200
            gainer_btn.color = Colors.BLACK
        else:
            mover_state["selected"] = "gainer"
            gainer_btn.bgcolor = Colors.GREEN
            gainer_btn.color = Colors.WHITE
            loser_btn.bgcolor = Colors.GREY_200
            loser_btn.color = Colors.BLACK
        render_mover_list()
        page.update()

    def select_losers(e):
        if mover_state["selected"] == "loser":
            mover_state["selected"] = None
            loser_btn.bgcolor = Colors.GREY_200
            loser_btn.color = Colors.BLACK
        else:
            mover_state["selected"] = "loser"
            loser_btn.bgcolor = Colors.RED
            loser_btn.color = Colors.WHITE
            gainer_btn.bgcolor = Colors.GREY_200
            gainer_btn.color = Colors.BLACK
        render_mover_list()
        page.update()

    gainer_btn = ft.ElevatedButton("Top 10 Gainers", bgcolor=Colors.GREY_200, color=Colors.BLACK, on_click=select_gainers, expand=True)
    loser_btn = ft.ElevatedButton("Top 10 Losers", bgcolor=Colors.GREY_200, color=Colors.BLACK, on_click=select_losers, expand=True)

    def refresh_analytics_screen():
        refresh_date_dropdown()
        render_mover_list()

    analytics_screen = ft.Container(
        padding=20,
        content=ft.Column(
            [
                ft.Text("Analytics Dashboard", size=24, weight=ft.FontWeight.BOLD),
                ft.Container(height=10),
                ft.Card(
                    content=ft.Container(
                        padding=15,
                        content=ft.Column(
                            [
                                ft.Row([analytics_date_text, analytics_date_dropdown],
                                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                                ft.Container(height=10),
                                ft.Row([gainer_btn, loser_btn], spacing=10),
                                ft.Divider(),
                                analytics_list_body,
                            ]
                        ),
                    )
                ),
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

    # ---- Full market universe / market-cap ranking controls ----
    universe_status_text = ft.Text("", size=12, color=Colors.GREY_600)

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
            universe_status_text.color = Colors.GREEN
            scan_universe_btn.disabled = False
            scan_universe_btn.text = "Scan Full Market (~2000 stocks)"
            page.update()

        threading.Thread(target=do_scan, daemon=True).start()

    scan_universe_btn = ft.ElevatedButton(
        "Scan Full Market (~2000 stocks)",
        icon=Icons.TRAVEL_EXPLORE,
        on_click=on_scan_universe,
    )

    market_cap_status_text = ft.Text(
        f"Last updated: {get_setting(db_conn, 'market_cap_last_updated', 'never')}",
        size=12, color=Colors.GREY_600,
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

    cap_btn = ft.ElevatedButton(
        "Update Market-Cap Ranking (Slow)",
        icon=Icons.LEADERBOARD,
        on_click=on_fetch_market_caps,
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
                ft.Text("Stock Search Coverage", size=18, weight=ft.FontWeight.W_600),
                ft.Text(
                    "Runs automatically on every 'Update Market Data' - use this only if you "
                    "want to refresh the search index without doing a full market sync.",
                    size=12, color=Colors.GREY_500,
                ),
                scan_universe_btn,
                universe_status_text,
                ft.Container(height=10),
                ft.Text(
                    "Ranks search results and analytics by market capitalisation. "
                    "This scans stocks one by one (no free bulk source exists) so it can take "
                    "15-30+ minutes for the full list - run it occasionally, not every day.",
                    size=12, color=Colors.GREY_500,
                ),
                cap_btn,
                market_cap_status_text,
                ft.Container(height=20),
                ft.Text("Dhan API (fastest, most reliable data source)", size=18, weight=ft.FontWeight.W_600),
                ft.Text(
                    "Free to get: open a Dhan account, then generate an Access Token from web.dhan.co. "
                    "Leave blank to use free public data sources instead (slower). "
                    "Fallback order: Dhan API -> NSE live data -> Yahoo Finance full scan.",
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
                    ft.Text("Version: 2.0.0 (StockAI Pro)"),
                    ft.Text("Database: Local SQLite"),
                    ft.Text("Data source order: Dhan API -> NSE live data -> Yahoo Finance (full scan fallback)"),
                    ft.Text("Search coverage: full NSE universe (~2000 stocks)"),
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
            # Populate the full ~2000 stock search index right away, so
            # search/watchlist-add works even before the user taps Sync.
            def universe_progress(msg):
                sync_status_text.value = msg
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
