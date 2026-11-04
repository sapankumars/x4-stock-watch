#!/usr/bin/env python3
"""
Radxa X4 stock watcher -> Telegram alerts

Setup:
    pip install requests
    1) Neeche BOT_TOKEN aur CHAT_ID bharo (ya env vars TG_BOT_TOKEN / TG_CHAT_ID set karo)
    2) python x4_stock_watch.py --test    -> Telegram par test message
    3) python x4_stock_watch.py --once    -> ek baar check, status print, loop nahi
    4) python x4_stock_watch.py           -> hamesha chalta rahega (har 1 ghante check)
    5) python x4_stock_watch.py --ci      -> GitHub Actions: ek baar check + alert, phir band
    6) python x4_stock_watch.py --pi      -> sirf Raspberry Pi (Pi 1/3/4/5) stock + price table, alert nahi

Alert sirf tab aata hai jab status "out of stock -> in stock" hota hai.
Agar koi site 3 baar lagataar fail ho (parsing toot gayi / site down), to ek warning bhi aayegi.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from html import unescape
from urllib.parse import quote

import requests
try:
    from curl_cffi import requests as cffi_requests  # Chrome jaisa TLS fingerprint
except ImportError:
    cffi_requests = None

# ----------------------------- CONFIG ---------------------------------------
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8757564755:AAEtVQ40ITDHCckoGFZG8Fp9HvgXe7dYE2A")
# Channel ke liye "@channelname" ya -100xxxxxxxxxx wala numeric id (bot channel mein admin hona chahiye)
CHAT_ID = os.environ.get("TG_CHAT_ID", "-1001946451020")

CHECK_EVERY_SEC = 3600     # 1 ghanta. Isse kam mat karna, sites block kar sakti hain
FAIL_WARN_AFTER = 3        # itni baar lagataar fail -> ek warning (3 x 1 ghanta)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x4_state.json")

# Shopify-type sites (ThinkRobotics, Allnet China) par sirf yeh RAM wale variants dekhe jayenge.
# 4GB hata diya hai kyunki Windows ke liye 8GB+ chahiye. 4GB bhi chahiye to "4gb" add kar do.
WANT_RAM = ["8gb", "12gb", "16gb"]

SITES = [
    {
        "name": "Hubtronics 8GB (no eMMC)",
        "url": "https://hubtronics.in/radxa-x4-8gb-no-emmc",
        "kind": "hubtronics",
    },
    {
        "name": "Hubtronics 16GB (no eMMC)",
        "url": "https://hubtronics.in/radxa-x4-16gb-no-emmc",
        "kind": "hubtronics",
    },
    {
        "name": "Evelta (variant page par check karna)",
        "url": "https://evelta.com/radxa-x4-credit-card-size-sbc/",
        "kind": "jsonld",
    },
    {
        "name": "Fab.to.Lab",
        "url": "https://www.fabtolab.com/radxa-x4-intel-processor-n100-lpddr5-ram-4gb-8gb-16gb",
        "kind": "fabtolab",
    },
    {
        "name": "ThinkRobotics",
        "url": "https://thinkrobotics.com/products/radxa-x4",
        "kind": "shopify",
    },
    {
        "name": "Allnet China (international, import duty lagegi)",
        "url": "https://shop.allnetchina.cn/products/radxa-x4",
        "kind": "shopify",
    },
    # Aur site add karni ho to yahan ek dict aur daal do (kind: hubtronics / jsonld / fabtolab / shopify)
]
# -----------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def http_get(url, retries=2, extra_headers=None):
    hdrs = dict(HEADERS, **(extra_headers or {}))
    for i in range(retries + 1):
        if cffi_requests is not None:
            cffi_hdrs = {k: v for k, v in hdrs.items() if k.lower() != "user-agent"}
            r = cffi_requests.get(url, headers=cffi_hdrs, timeout=25, impersonate="chrome")
        else:
            r = requests.get(url, headers=hdrs, timeout=25)
        if r.status_code == 429 and i < retries:
            try:
                wait = int(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 5 * (i + 1)
            time.sleep(min(wait, 30))
            continue
        r.raise_for_status()
        return r


def to_text(html):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


# ------------------------- per-site stock checkers ---------------------------
# Har checker (in_stock: bool | None, detail: str) return karta hai. None = samajh nahi aaya.

def parse_jsonld(html):
    m = re.search(
        r'"availability"\s*:\s*"[^"]*?(InStock|LimitedAvailability|OutOfStock|SoldOut|PreOrder|BackOrder|Discontinued)"',
        html,
        re.I,
    )
    if not m:
        return None, "availability tag nahi mila"
    val = m.group(1)
    return val.lower() in ("instock", "limitedavailability"), val


def ram_gb(text):
    """Text mein pehla 'N GB' / 'N-GB' (1-2 digit) RAM maana jata hai. Nahi mila -> None."""
    m = re.search(r"(?<!\d)(\d{1,2})\s*-?\s*gb", (text or "").lower())
    return int(m.group(1)) if m else None


WANT_GB = {n for n in (ram_gb(w) for w in WANT_RAM) if n}


def _ram_set(text):
    return set(int(x) for x in re.findall(r"(?<!\d)(\d{1,2})\s*-?\s*gb", (text or "").lower()))


def _walk_ld(node, ram, out):
    if isinstance(node, list):
        for x in node:
            _walk_ld(x, ram, out)
    elif isinstance(node, dict):
        own = " ".join(str(node.get(k)) for k in ("name", "sku", "mpn", "url")
                       if isinstance(node.get(k), (str, int)))
        rs = _ram_set(own)
        if len(rs) == 1:
            ram = next(iter(rs))
        if "availability" in node:
            out.append((ram, str(node["availability"])))
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk_ld(v, ram, out)


def parse_jsonld_ram(html):
    """JSON-LD ke har offer ko uski RAM se jodkar WANT_RAM ke hisaab se check karta hai.
    RAM-wise offers na mile to (None, ...) -> caller fallback karega."""
    offers = []
    for blk in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            _walk_ld(json.loads(blk), None, offers)
        except ValueError:
            continue
    tagged = [(r, a) for r, a in offers if r is not None]
    if not tagged:
        return None, "JSON-LD mein RAM-wise offers nahi mile"
    wanted = [(r, a) for r, a in tagged if not WANT_GB or r in WANT_GB]
    if not wanted:
        return False, "wanted RAM ka offer page par nahi hai"
    ins = sorted({f"{r}GB" for r, a in wanted if re.search(r"InStock|LimitedAvailability", a, re.I)})
    if ins:
        return True, ", ".join(ins)
    return False, "wanted RAM out of stock"


def parse_jsonld_variants(html):
    """Pehle RAM-aware JSON-LD; na ho paye to purana pehla-tag logic, par detail mein flag."""
    r = parse_jsonld_ram(html)
    if r[0] is not None:
        return r
    st, d = parse_jsonld(html)
    return (st, d + " (RAM verify nahi hua)") if st is not None else (st, d)


def parse_hubtronics(html):
    # Hubtronics page par "Stock: 0 in Stock" jaisa text hota hai
    m = re.search(r"Stock:\s*(\d+)\s*in\s*stock", to_text(html), re.I) or re.search(
        r"Stock:?\**\s*(\d+)\s*in\s*stock", html, re.I
    )
    if m:
        n = int(m.group(1))
        return n > 0, f"{n} in stock"
    return parse_jsonld_variants(html)


def parse_fabtolab(html):
    r = parse_jsonld_ram(html)  # RAM-wise offers mil jayein to wahi bharosemand hai
    if r[0] is not None:
        return r
    text = to_text(html)
    m = re.search(r"Availability:\s*:?\s*(.{0,25})", text, re.I)
    if not m:
        return parse_jsonld_variants(html)
    seg = m.group(1).lower()
    flag = " (default variant ka status, RAM verify nahi hua)"
    if "out of stock" in seg or "out-of-stock" in seg or "pre-order" in seg:
        return False, seg.strip() + flag
    if "in stock" in seg or re.match(r"\d+", seg.strip()):
        return True, seg.strip() + flag
    return None, f"samajh nahi aaya: {seg!r}"


def parse_shopify(js):
    """js = Shopify product .js endpoint ka JSON (dict).
    RAM variant title / sku se nikalta hai ("8GB", "8 GB", "8-GB" sab chalte hain).
    Variant mein RAM nahi (jaise "Default Title") to product title se; wo bhi na mile to
    variant ko rakhte hain par detail mein flag lagate hain."""
    avail, unknown = [], False
    prod_ram = ram_gb(js.get("title"))
    for v in js.get("variants", []):
        r = ram_gb(f"{v.get('title') or ''} {v.get('sku') or ''}")
        if r is None:
            r = prod_ram
        if WANT_RAM and r is not None and r not in WANT_GB:
            continue
        if WANT_RAM and r is None:
            unknown = True
        if v.get("available"):
            avail.append(v.get("title") or "variant")
    if avail:
        return True, ", ".join(avail) + (" (RAM info nahi mila, check karo)" if unknown else "")
    return False, "wanted variants available nahi"


def check_site(site):
    kind = site["kind"]
    if kind == "shopify":
        base = site["url"].rstrip("/")
        try:
            return parse_shopify(http_get(base + ".js").json())
        except Exception:
            # .js block ho gaya to normal page ka availability tag try karo
            return parse_jsonld_variants(http_get(base).text)
    if kind == "hubtronics":
        try:
            html = http_get(site["url"]).text
        except Exception as e:
            if "403" not in str(e):
                raise
            html = http_get(
                "https://r.jina.ai/" + site["url"], extra_headers={"X-No-Cache": "true"}
            ).text
        return parse_hubtronics(html)
    html = http_get(site["url"]).text
    if kind == "fabtolab":
        return parse_fabtolab(html)
    return parse_jsonld_variants(html)


# ------------------------------ Telegram -------------------------------------
def tg_send(text):
    if "PASTE_BOT_TOKEN" in BOT_TOKEN:
        log("BOT_TOKEN set nahi hai, Telegram message skip: " + text.splitlines()[0])
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=20,
        )
        if not r.ok:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
            return False
        log(f"Telegram OK (chat {CHAT_ID}): {text.splitlines()[0]}")
        return True
    except requests.RequestException as e:
        log(f"Telegram request fail: {e}")
        return False


# ------------------------------- state ---------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        log(f"state save fail: {e}")



# ------------------- Raspberry Pi (India ke authorised sellers) ---------------
# Sirf Pi 1 / 3 / 4 / 5 boards. Zero, Pico, Compute Module, 400/500, case, power supply,
# camera, kit wagairah chhod diye jaate hain.
PI_SELLERS = [
    {"name": "ThinkRobotics", "kind": "shopify", "base": "https://thinkrobotics.com", "collection": "raspberry-pi"},
    {"name": "Robocraze", "kind": "shopify", "base": "https://robocraze.com", "collection": "raspberry-pi"},
    {"name": "Robu.in", "kind": "woo", "base": "https://robu.in"},
    {"name": "AryaBOT", "kind": "woo", "base": "https://aryabot.in"},
    {"name": "Thingbits", "kind": "spree", "base": "https://thingbits.in",
     "pages": [
         "https://thingbits.in/t/brands/raspberry-pi/boards",
         "https://thingbits.in/t/brands/raspberry-pi/boards?page=2",
     ]},
]
PI_MODELS = [  # order matters: pehle 5, 4, 3 check hota hai, phir Pi 1
    ("Pi 5", r"pi\s*5\b"),
    ("Pi 4", r"pi\s*4\b"),
    ("Pi 3", r"pi\s*3\b"),
    ("Pi 1", r"pi\s*1\b|pi\s*model\s*[ab]"),
]
PI_EXCLUDE = re.compile(
    r"\b(case|power|supply|adapter|adaptor|charger|cable|camera|hat|display|screen|monitor|keyboard|"
    r"mouse|fan|cooler|heatsink|kit|desktop|bundle|combo|enclosure|shield|zero|pico|compute|module|"
    r"sd card|memory card|ssd|nvme)\b",
    re.I,
)


def pi_classify(name):
    n = (name or "").strip().lower()
    if not re.match(r"^(official\s+)?raspberry\s*pi", n) or PI_EXCLUDE.search(n):
        return None
    for label, rx in PI_MODELS:
        if re.search(rx, n):
            return label
    return None


def to_price(x):
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


def pi_shopify(s):
    base = s["base"]
    out = []
    try:
        js = http_get(f"{base}/collections/{s['collection']}/products.json?limit=250").json()
        for p in js.get("products", []):
            for v in p.get("variants", []):
                vt = v.get("title") or ""
                title = p.get("title", "")
                name = title if vt.lower() == "default title" else f"{title} - {vt}"
                out.append({
                    "name": name,
                    "url": f"{base}/products/{p.get('handle')}",
                    "price": to_price(v.get("price")),
                    "in_stock": bool(v.get("available")),
                })
    except Exception:
        out = []
    if not any(pi_classify(o["name"]) for o in out):
        # collection endpoint nahi chala -> Shopify search suggest endpoint
        out = []
        for q in ("raspberry pi 5", "raspberry pi 4", "raspberry pi 3", "raspberry pi model b"):
            r = http_get(
                f"{base}/search/suggest.json?q={quote(q)}&resources[type]=product&resources[limit]=10"
            ).json()
            for p in r.get("resources", {}).get("results", {}).get("products", []):
                out.append({
                    "name": p.get("title", ""),
                    "url": base + (p.get("url") or "").split("?")[0],
                    "price": to_price(p.get("price")),
                    "in_stock": bool(p.get("available")),
                })
            time.sleep(random.uniform(1, 2))
    return out


def pi_woo(s):
    base = s["base"]
    data = http_get(f"{base}/wp-json/wc/store/v1/products?search=raspberry%20pi&per_page=100").json()
    out = []
    for p in data:
        pr = p.get("prices") or {}
        price = None
        try:
            price = int(pr.get("price")) / (10 ** int(pr.get("currency_minor_unit", 2)))
        except (TypeError, ValueError):
            pass
        out.append({
            "name": unescape(p.get("name", "")),
            "url": p.get("permalink") or base,
            "price": price,
            "in_stock": bool(p.get("is_in_stock")),
        })
    return out


def parse_text_listing(text, page_url):
    """Listing page ke text se: <name> SKU: ... In Stock / Out of Stock (+ price agar dikhe)."""
    out = []
    parts = re.split(r"(?=SKU:)", text)
    for i in range(1, len(parts)):
        before, after = parts[i - 1], parts[i]
        starts = [m.start() for m in re.finditer(r"Raspberry Pi", before)]
        if not starts:
            continue
        seg = before[starts[-1]:]
        name = re.split(r"₹|Rs\.?|SKU", seg)[0].strip()
        after_cut = after.split("Raspberry Pi")[0]
        st = re.search(r"(Out of Stock|Sold Out|In Stock)", after_cut, re.I)
        pm = re.search(r"(?:₹|Rs\.?)\s*([\d,]+(?:\.\d+)?)", seg + " " + after_cut)
        out.append({
            "name": name,
            "url": page_url,
            "price": to_price(pm.group(1)) if pm else None,
            "in_stock": (None if not st else st.group(1).lower() == "in stock"),
        })
    return out


def pi_spree(s):
    out = []
    for i, url in enumerate(s["pages"]):
        try:
            out += parse_text_listing(to_text(http_get(url).text), url)
        except Exception:
            if i == 0:
                raise
        time.sleep(random.uniform(1, 2))
    return out


def pi_collect():
    """Return (items, errors). items: list of dict(seller, model, name, url, price, in_stock)."""
    items, errors, seen = [], {}, set()
    for s in PI_SELLERS:
        time.sleep(random.uniform(2, 5))
        try:
            fetch = {"shopify": pi_shopify, "woo": pi_woo}.get(s["kind"], pi_spree)
            found = []
            for o in fetch(s):
                model = pi_classify(o["name"])
                k = (s["name"], o["name"])
                if model and k not in seen:
                    seen.add(k)
                    o.update(seller=s["name"], model=model)
                    found.append(o)
            if not found:
                raise ValueError("koi Pi board nahi mila (site structure badal gayi ho sakti hai)")
            items += found
        except Exception as e:
            errors[s["name"]] = str(e)
    return items, errors


def pi_price_txt(p):
    return f"Rs {p:,.0f}" if p else "price ?"


def pi_print(items, errors):
    for label, _ in PI_MODELS:
        rows = [o for o in items if o["model"] == label]
        if not rows:
            log(f"=== {label}: kisi seller par nahi mila ===")
            continue
        log(f"=== {label} ===")
        for o in sorted(rows, key=lambda o: (o["seller"], o["name"])):
            st = "IN STOCK" if o["in_stock"] else ("out of stock" if o["in_stock"] is False else "UNKNOWN")
            log(f"  [{o['seller']}] {o['name']} | {pi_price_txt(o['price'])} | {st}")
    for seller, e in errors.items():
        log(f"  ERROR {seller}: {e}")


def run_pi_cycle(state, fails):
    items, errors = pi_collect()
    pi_print(items, errors)

    for s in PI_SELLERS:
        fk = f"pi-fail|{s['name']}"
        if s["name"] in errors:
            fails[fk] = fails.get(fk, 0) + 1
            if fails[fk] == FAIL_WARN_AFTER:
                tg_send(f"⚠️ Pi watcher: {s['name']} ka data {FAIL_WARN_AFTER} baar nahi padh paya.\n{errors[s['name']]}")
        else:
            fails[fk] = 0

    alerts = []  # (state_key, new_value, text)
    for o in items:
        if o["in_stock"] is None:
            continue
        key = f"pi|{o['seller']}|{o['name']}"
        new = {"in_stock": o["in_stock"], "price": o["price"]}
        old = state.get(key) if isinstance(state.get(key), dict) else {}
        was = old.get("in_stock") is True
        old_price = old.get("price")
        text = None
        if o["in_stock"] and not was:
            text = (f"🟢 {o['model']} STOCK MEIN: {o['name']}\n"
                    f"{o['seller']} | {pi_price_txt(o['price'])}\n{o['url']}")
        elif o["in_stock"] and was and o["price"] and old_price and o["price"] < old_price * 0.99:
            text = (f"💸 {o['model']} PRICE GIRA: {o['name']}\n"
                    f"{o['seller']} | {pi_price_txt(old_price)} -> {pi_price_txt(o['price'])}\n{o['url']}")
        if text:
            alerts.append((key, new, text))
        else:
            state[key] = new

    # Telegram limit 4096 chars: alerts ko chhote batches mein bhejo.
    # State sirf tab update hota hai jab message sach mein chala gaya (nahi to agle run mein dobara try).
    batch, size = [], 0
    for item in alerts + [None]:
        if item is None or (batch and size + len(item[2]) > 3500):
            if batch:
                if tg_send("\n\n".join(t for _, _, t in batch)):
                    for k, v, _ in batch:
                        state[k] = v
                batch, size = [], 0
        if item is not None:
            batch.append(item)
            size += len(item[2]) + 2
    save_state(state)


# ------------------------------- main loop -----------------------------------
def run_cycle(state, fails, notify=True):
    for site in SITES:
        name = site["name"]
        time.sleep(random.uniform(2, 5))
        try:
            in_stock, detail = check_site(site)
        except Exception as e:  # network, parsing, kuch bhi
            in_stock, detail = None, f"error: {e}"

        if in_stock is None:
            fails[name] = fails.get(name, 0) + 1
            log(f"{name}: UNKNOWN ({detail}) [fail #{fails[name]}]")
            if notify and fails[name] == FAIL_WARN_AFTER:
                tg_send(
                    f"⚠️ X4 watcher: {name} ka status {FAIL_WARN_AFTER} baar nahi padh paya.\n"
                    f"{detail}\n{site['url']}"
                )
            continue

        fails[name] = 0
        log(f"{name}: {'IN STOCK' if in_stock else 'out of stock'} ({detail})")
        was_in_stock = state.get(name) is True
        state[name] = in_stock
        if notify and in_stock and not was_in_stock:
            tg_send(
                f"🟢 Radxa X4 STOCK MEIN AAYA!\n{name}\n{detail}\n{site['url']}"
            )
    save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="Telegram test message bhejo")
    ap.add_argument("--once", action="store_true", help="ek baar check karke band")
    ap.add_argument("--ci", action="store_true", help="GitHub Actions: ek baar check + alert, phir band")
    ap.add_argument("--pi", action="store_true", help="sirf Raspberry Pi stock + price table print karo (alert/state nahi)")
    args = ap.parse_args()

    if args.test:
        ok = tg_send("✅ X4 stock watcher: test message aa gaya.")
        sys.exit(0 if ok else 1)

    if args.pi:
        items, errors = pi_collect()
        pi_print(items, errors)
        return

    state = load_state()
    fails = state.pop("_fails", {}) if args.ci else {}

    if args.once:
        run_cycle(state, fails, notify=False)
        items, errors = pi_collect()
        pi_print(items, errors)
        return

    if args.ci:
        run_cycle(state, fails)
        run_pi_cycle(state, fails)
        state["_fails"] = fails
        save_state(state)
        return

    log(f"Watcher chalu. {len(SITES)} sites, har {CHECK_EVERY_SEC // 60} min mein check.")
    tg_send(f"👀 X4 stock watcher chalu ho gaya ({len(SITES)} sites monitor ho rahi hain).")
    try:
        while True:
            run_cycle(state, fails)
            run_pi_cycle(state, fails)
            time.sleep(CHECK_EVERY_SEC + random.randint(0, 60))
    except KeyboardInterrupt:
        log("Band kiya.")


if __name__ == "__main__":
    main()