
from flask import Flask, render_template, request
import requests
from bs4 import BeautifulSoup
import re
import sqlite3
import socket
import ipaddress
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path

app = Flask(__name__)
DATABASE = Path(__file__).with_name("pricepulse.db")


# ---------------- DATABASE ----------------

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                target_price REAL NOT NULL,
                current_price REAL NOT NULL,
                checked_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                price REAL NOT NULL,
                checked_at TEXT NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id)
            )
        """)


# ---------------- URL VALIDATION ----------------

def validate_public_url(url):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(
            "Enter a valid product URL starting with https://."
        )

    hostname = parsed.hostname.lower()

    if hostname == "localhost" or hostname.endswith(".local"):
        raise ValueError("Please enter a public product webpage URL.")

    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError("The website address could not be found.")

    if not addresses:
        raise ValueError("The website address could not be found.")

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("This URL does not point to a public address.")


# ---------------- PRICE SCRAPER ----------------

def clean_price(price_text):
    cleaned = re.sub(r"[^\d,.]", "", str(price_text))

    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        else:
            cleaned = "".join(parts)

    try:
        price = float(cleaned)
        return price if price > 0 else None
    except ValueError:
        return None


def scrape_price(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=20,
        allow_redirects=True
    )
    response.raise_for_status()

    # Verify the final destination after redirects.
    validate_public_url(response.url)

    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else urlparse(url).netloc

    selectors = [
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
        '[itemprop="price"]',
        '[data-price]',
        '.price',
        '#price',
    ]

    for selector in selectors:
        element = soup.select_one(selector)

        if element:
            text = (
                element.get("content")
                or element.get("data-price")
                or element.get_text(" ", strip=True)
            )
            price = clean_price(text)

            if price is not None:
                return price, title

    # Fallback: search visible page text for common currencies.
    text = soup.get_text(" ", strip=True)

    matches = re.findall(
        r"(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d{1,2})?",
        text,
        flags=re.IGNORECASE,
    )

    for match in matches:
        price = clean_price(match)
        if price is not None:
            return price, title

    raise ValueError(
        "No reliable price was found. This store may block automated "
        "requests or require a different method to read its price."
    )


# ---------------- SAVE PRICE ----------------

def save_price(url, name, target_price, live_price):
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        conn.execute("""
            INSERT INTO products
                (url, name, target_price, current_price, checked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                name = excluded.name,
                target_price = excluded.target_price,
                current_price = excluded.current_price,
                checked_at = excluded.checked_at
        """, (url, name, target_price, live_price, checked_at))

        product = conn.execute(
            "SELECT id FROM products WHERE url = ?", (url,)
        ).fetchone()

        conn.execute("""
            INSERT INTO price_history (product_id, price, checked_at)
            VALUES (?, ?, ?)
        """, (product["id"], live_price, checked_at))


# ---------------- DASHBOARD DATA ----------------

def get_dashboard_data():
    with get_db() as conn:
        stats = conn.execute("""
            SELECT
                COUNT(*) AS total_products,
                COALESCE(SUM(
                    CASE WHEN current_price <= target_price
                    THEN 1 ELSE 0 END
                ), 0) AS deals,
                COALESCE(SUM(
                    CASE WHEN current_price <= target_price
                    THEN target_price - current_price ELSE 0 END
                ), 0) AS potential_savings
            FROM products
        """).fetchone()

        price_checks = conn.execute(
            "SELECT COUNT(*) FROM price_history"
        ).fetchone()[0]

        watchlist = conn.execute("""
            SELECT *
            FROM products
            ORDER BY checked_at DESC
        """).fetchall()

        history = conn.execute("""
            SELECT p.name, p.url, h.price, h.checked_at
            FROM price_history h
            JOIN products p ON p.id = h.product_id
            ORDER BY h.id DESC
            LIMIT 20
        """).fetchall()

    return {
        "total_products": stats["total_products"],
        "deals": stats["deals"],
        "price_checks": price_checks,
        "potential_savings": stats["potential_savings"],
        "watchlist": watchlist,
        "history": history,
    }


# ---------------- MAIN PAGE ----------------

@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error = None

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        target_text = request.form.get("target_price", "").strip()

        try:
            if not url or not target_text:
                raise ValueError(
                    "Please enter both a product URL and a target price."
                )

            try:
                target_price = float(target_text)
            except ValueError:
                raise ValueError("Enter a valid number for your target price.")

            if target_price <= 0:
                raise ValueError("Target price must be greater than zero.")

            validate_public_url(url)
            live_price, product_name = scrape_price(url)

            save_price(url, product_name, target_price, live_price)

            result = {
                "url": url,
                "name": product_name,
                "live_price": live_price,
                "target_price": target_price,
                "difference": live_price - target_price,
                "is_deal": live_price <= target_price,
            }

        except (ValueError, requests.RequestException) as exc:
            error = str(exc) or "We couldn't retrieve this product's price."
        except Exception:
            app.logger.exception("Unexpected price-checking error")
            error = "Something went wrong. Please check the terminal for details."

    dashboard = get_dashboard_data()

    return render_template(
        "index.html",
        result=result,
        error=error,
        **dashboard
    )


@app.route("/health")
def health():
    return {"status": "ok"}


# ---------------- START SERVER ----------------

init_db()

if __name__ == "__main__":
    app.run(debug=True)
