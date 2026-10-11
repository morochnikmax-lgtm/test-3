#!/usr/bin/env python3
"""Unternehmer-Dashboard: Kiosk (Vectron) + Automaten (Televend).

Start:  python3 server.py            (Port aus config.json)
Demo:   python3 server.py --demo     (Beispieldaten erzeugen)
Daten fließen per CSV-Export in inbox/vectron bzw. inbox/televend
(oder per POST /api/ingest/<quelle>). Reine Python-Standardbibliothek.
"""
import csv, hashlib, io, json, os, random, re, sqlite3, sys, threading, time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
DB = os.path.join(BASE, "dashboard.db")
LOCK = threading.Lock()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sales(
          uid TEXT PRIMARY KEY, source TEXT, location TEXT, ts TEXT,
          article TEXT, category TEXT, qty REAL, amount REAL, payment TEXT);
        CREATE INDEX IF NOT EXISTS i_ts ON sales(ts);
        CREATE TABLE IF NOT EXISTS imports(
          source TEXT, file TEXT, at TEXT, rows INTEGER, new_rows INTEGER);
        """)


# ---------- Import ----------
def num(s):
    s = str(s or "").strip().replace("€", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s:  # deutsches Format 1.234,56
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


DATE_FORMATS = ["%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%d.%m.%y %H:%M", "%d.%m.%Y"]


def parse_ts(s):
    s = str(s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return None


def pick(row, aliases):
    for a in aliases:
        if a in row and row[a] != "":
            return row[a]
    return ""


def ingest_csv(source, text, filename="upload"):
    scfg = CFG["sources"][source]
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    cols = scfg["columns"]
    rows, total = [], 0
    for raw in reader:
        total += 1
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        ts = parse_ts(pick(row, cols["time"]))
        if not ts:
            continue
        qty = num(pick(row, cols["qty"])) or 1.0
        amount = num(pick(row, cols["amount"]))
        article = pick(row, cols["article"]) or "Unbekannt"
        rid = pick(row, cols["id"])
        loc = pick(row, cols["location"]) or scfg["default_location"]
        uid = hashlib.sha1(f"{source}|{loc}|{rid}|{ts}|{article}|{qty}|{amount}".encode()).hexdigest()
        rows.append((uid, source, loc, ts, article, pick(row, cols["category"]) or "Sonstiges",
                     qty, amount, pick(row, cols["payment"]) or "n/a"))
    with LOCK, db() as c:
        before = c.total_changes
        c.executemany("INSERT OR IGNORE INTO sales VALUES(?,?,?,?,?,?,?,?,?)", rows)
        new = c.total_changes - before
        c.execute("INSERT INTO imports VALUES(?,?,?,?,?)",
                  (source, filename, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), total, new))
    return {"rows": total, "new": new}


def scan_inboxes():
    for source, scfg in CFG["sources"].items():
        folder = os.path.join(BASE, scfg["inbox"])
        os.makedirs(folder, exist_ok=True)
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith((".csv", ".txt")):
                continue
            path = os.path.join(folder, name)
            with LOCK, db() as c:
                seen = c.execute("SELECT 1 FROM imports WHERE source=? AND file=?",
                                 (source, f"{name}:{os.path.getmtime(path):.0f}")).fetchone()
            if seen:
                continue
            raw = open(path, "rb").read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp1252")
            res = ingest_csv(source, text, f"{name}:{os.path.getmtime(path):.0f}")
            print(f"[import] {source}/{name}: {res}")


# ---------- Automatische Abholung ----------
def secret(cfg, key):
    return os.environ.get(cfg.get(key + "_env", ""), "") or cfg.get(key, "")


def fetch_imap(source, f):
    """Liest ungelesene Mails mit CSV-Anhang (Berichtsversand von Vectron/Televend)."""
    import imaplib, email
    m = imaplib.IMAP4_SSL(f["host"], f.get("port", 993))
    m.login(f["user"], secret(f, "password"))
    m.select(f.get("folder", "INBOX"))
    crit = ["UNSEEN"] + (["FROM", f'"{f["from_contains"]}"'] if f.get("from_contains") else []) \
        + (["SUBJECT", f'"{f["subject_contains"]}"'] if f.get("subject_contains") else [])
    _, ids = m.search(None, *crit)
    for i in ids[0].split():
        _, data = m.fetch(i, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])
        ok = True
        for part in msg.walk():
            name = part.get_filename() or ""
            if name.lower().endswith((".csv", ".txt")):
                raw = part.get_payload(decode=True)
                try: text = raw.decode("utf-8")
                except UnicodeDecodeError: text = raw.decode("cp1252")
                print(f"[mail] {source}/{name}: {ingest_csv(source, text, 'mail:' + name)}")
        m.store(i, "+FLAGS", "\\Seen")
    m.logout()


def fetch_ftp(source, f):
    """Lädt neue CSV-Dateien von einem FTP-/FTPS-Ordner in die Inbox."""
    import ftplib
    ftp = (ftplib.FTP_TLS if f.get("tls") else ftplib.FTP)(f["host"], f.get("port", 21), timeout=30)
    ftp.login(f["user"], secret(f, "password"))
    if f.get("tls"): ftp.prot_p()
    ftp.cwd(f.get("path", "/"))
    dest = os.path.join(BASE, CFG["sources"][source]["inbox"])
    for name in ftp.nlst():
        if name.lower().endswith((".csv", ".txt")) and not os.path.exists(os.path.join(dest, name)):
            with open(os.path.join(dest, name), "wb") as fh:
                ftp.retrbinary("RETR " + name, fh.write)
    ftp.quit()


def fetch_http(source, f):
    """Ruft eine URL ab (CSV-Export-Link / Cloud-Schnittstelle mit Token)."""
    import urllib.request
    url = f["url"].replace("{from}", (datetime.now() - timedelta(days=f.get("days_back", 3))).strftime("%Y-%m-%d")) \
                  .replace("{to}", datetime.now().strftime("%Y-%m-%d"))
    req = urllib.request.Request(url)
    if secret(f, "token"):
        req.add_header(f.get("header", "Authorization"), f.get("prefix", "Bearer ") + secret(f, "token"))
    raw = urllib.request.urlopen(req, timeout=60).read()
    try: text = raw.decode("utf-8")
    except UnicodeDecodeError: text = raw.decode("cp1252")
    print(f"[http] {source}: {ingest_csv(source, text, 'http-pull')}")


FETCHERS = {"imap": fetch_imap, "ftp": fetch_ftp, "http": fetch_http}


def fetch_all():
    for source, scfg in CFG["sources"].items():
        for f in scfg.get("fetch", []):
            if f.get("enabled", True):
                try:
                    FETCHERS[f["type"]](source, f)
                except Exception as e:
                    print(f"[fetch] {source}/{f['type']} Fehler: {e}")


def import_loop():
    while True:
        try:
            fetch_all()
            scan_inboxes()
        except Exception as e:  # nie den Server stoppen
            print("[import] Fehler:", e)
        time.sleep(CFG["import_interval_seconds"])


# ---------- Auswertung ----------
def rng(q):
    today = datetime.now().date()
    d_to = q.get("to", [str(today)])[0]
    d_from = q.get("from", [str(today - timedelta(days=29))])[0]
    return d_from, d_to


def summary(q):
    d_from, d_to = rng(q)
    f = datetime.strptime(d_from, "%Y-%m-%d")
    t = datetime.strptime(d_to, "%Y-%m-%d")
    days = (t - f).days + 1
    pf = (f - timedelta(days=days)).strftime("%Y-%m-%d")
    pt = (f - timedelta(days=1)).strftime("%Y-%m-%d")
    where = "substr(ts,1,10) BETWEEN ? AND ?"
    c = db()

    def kpi(a, b):
        r = {}
        for r_ in c.execute(f"""SELECT source, SUM(amount) rev, COUNT(DISTINCT substr(ts,1,16)||location) tx,
                                SUM(qty) units FROM sales WHERE {where} GROUP BY source""", (a, b)):
            r[r_["source"]] = {"revenue": r_["rev"] or 0, "tx": r_["tx"], "units": r_["units"] or 0}
        return r

    today = str(datetime.now().date())
    out = {
        "range": {"from": d_from, "to": d_to, "days": days},
        "current": kpi(d_from, d_to), "previous": kpi(pf, pt), "today": kpi(today, today),
        "daily": [dict(r) for r in c.execute(
            f"""SELECT substr(ts,1,10) day, source, SUM(amount) revenue FROM sales
                WHERE {where} GROUP BY day, source ORDER BY day""", (d_from, d_to))],
        "hourly": [dict(r) for r in c.execute(
            f"""SELECT CAST(substr(ts,12,2) AS INT) hour, source, SUM(amount) revenue FROM sales
                WHERE {where} GROUP BY hour, source ORDER BY hour""", (d_from, d_to))],
        "top_articles": [dict(r) for r in c.execute(
            f"""SELECT source, article, SUM(qty) units, SUM(amount) revenue FROM sales
                WHERE {where} GROUP BY source, article ORDER BY revenue DESC LIMIT 12""", (d_from, d_to))],
        "locations": [dict(r) for r in c.execute(
            f"""SELECT source, location, SUM(amount) revenue, SUM(qty) units, MAX(ts) last_sale FROM sales
                WHERE {where} GROUP BY source, location ORDER BY revenue DESC""", (d_from, d_to))],
        "categories": [dict(r) for r in c.execute(
            f"""SELECT source, category, SUM(amount) revenue FROM sales
                WHERE {where} GROUP BY source, category ORDER BY revenue DESC""", (d_from, d_to))],
        "payments": [dict(r) for r in c.execute(
            f"""SELECT source, payment, SUM(amount) revenue FROM sales
                WHERE {where} GROUP BY source, payment ORDER BY revenue DESC""", (d_from, d_to))],
        "freshness": {r["source"]: {"last_import": r["at"], "last_sale": r["ls"]} for r in c.execute(
            """SELECT i.source, MAX(i.at) at, (SELECT MAX(ts) FROM sales s WHERE s.source=i.source) ls
               FROM imports i GROUP BY i.source""")},
        "labels": {k: v["label"] for k, v in CFG["sources"].items()},
    }
    c.close()
    return out


# ---------- Demo-Daten ----------
def make_demo(days=60):
    random.seed(7)
    kiosk = [("Kaffee", "Heißgetränke", 2.5), ("Cappuccino", "Heißgetränke", 3.2), ("Brötchen belegt", "Snacks", 3.9),
             ("Croissant", "Backwaren", 2.2), ("Zeitung", "Presse", 2.8), ("Obstschale", "Snacks", 2.9),
             ("Mineralwasser", "Kaltgetränke", 1.8), ("Schokoriegel", "Süßwaren", 1.5), ("Blumenstrauß", "Geschenke", 12.9)]
    auto = [("Cola 0,5l", "Getränke", 2.2), ("Wasser 0,5l", "Getränke", 1.6), ("Apfelschorle", "Getränke", 2.0),
            ("Chips", "Snacks", 1.9), ("Müsliriegel", "Snacks", 1.4), ("Schokolade", "Süßwaren", 1.7),
            ("Nüsse", "Snacks", 2.3), ("Energy Drink", "Getränke", 2.6)]
    machines = ["Automat Eingang", "Automat Station 3", "Automat Notaufnahme", "Automat Wartebereich"]
    wk = {0: 1.0, 1: 1.05, 2: 1.0, 3: 1.0, 4: 0.95, 5: 0.6, 6: 0.55}
    kf, af = io.StringIO(), io.StringIO()
    kw = csv.writer(kf, delimiter=";"); aw = csv.writer(af, delimiter=";")
    kw.writerow(["Bonnummer", "Datum/Zeit", "Artikel", "Warengruppe", "Menge", "Umsatz", "Zahlart", "Kasse"])
    aw.writerow(["Verkaufsnr", "Zeit", "Produkt", "Warengruppe", "Menge", "Preis", "Zahlart", "Automat"])
    now = datetime.now().replace(second=0, microsecond=0)
    n = 0
    for d in range(days, -1, -1):
        day = now - timedelta(days=d)
        f = wk[day.weekday()] * (1 + (days - d) * 0.002)
        for _ in range(int(random.gauss(95, 8) * f)):
            h = random.choices(range(6, 21), [3, 8, 12, 10, 7, 9, 11, 8, 6, 5, 5, 4, 2, 1, 1])[0]
            ts = day.replace(hour=h, minute=random.randint(0, 59))
            if ts > now: continue
            n += 1
            for _ in range(random.choice([1, 1, 1, 2, 3])):
                a, cat, p = random.choice(kiosk); q = random.choice([1, 1, 1, 2])
                kw.writerow([f"K{n}", ts.strftime("%d.%m.%Y %H:%M"), a, cat, q,
                             f"{p*q:.2f}".replace(".", ","), random.choice(["Bar", "Karte", "Karte", "EC"]), "Kiosk"])
        for _ in range(int(random.gauss(70, 9) * f)):
            h = random.choices(range(0, 24), [2,1,1,1,1,2,4,6,7,6,5,5,6,6,5,5,5,5,5,4,3,3,2,2])[0]
            ts = day.replace(hour=h, minute=random.randint(0, 59))
            if ts > now: continue
            n += 1
            a, cat, p = random.choice(auto)
            aw.writerow([f"A{n}", ts.strftime("%d.%m.%Y %H:%M:%S"), a, cat, 1, f"{p:.2f}".replace(".", ","),
                         random.choice(["Münzen", "Karte", "Karte", "Handy"]), random.choice(machines)])
    ingest_csv("vectron", kf.getvalue(), f"demo-vectron:{time.time():.0f}")
    ingest_csv("televend", af.getvalue(), f"demo-televend:{time.time():.0f}")
    print("Demo-Daten erzeugt.")


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/summary":
            return self.send(200, json.dumps(summary(parse_qs(u.query))))
        if u.path in ("/", "/index.html"):
            return self.send(200, open(os.path.join(BASE, "web", "index.html"), "rb").read(), "text/html")
        self.send(404, "{}")

    def do_POST(self):
        m = re.fullmatch(r"/api/ingest/(\w+)", urlparse(self.path).path)
        token = CFG.get("ingest_token") or os.environ.get("DASHBOARD_TOKEN")
        if token and self.headers.get("Authorization") != f"Bearer {token}":
            return self.send(401, '{"error":"unauthorized"}')
        if not m or m.group(1) not in CFG["sources"]:
            return self.send(404, '{"error":"unknown source"}')
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send(200, json.dumps(ingest_csv(m.group(1), body.decode("utf-8", "replace"), "http-post")))


if __name__ == "__main__":
    init_db()
    if "--demo" in sys.argv:
        make_demo()
    scan_inboxes()
    threading.Thread(target=import_loop, daemon=True).start()
    port = int(os.environ.get("PORT", CFG["port"]))
    print(f"Dashboard läuft auf http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
