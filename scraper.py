"""
SSD Price Tracker - kakaku.com scraper
Pattern follows the GPU tracker: kakaku listing -> item detail page -> authorized-retailer lowest price.

Usage:
    python scraper.py              # normal run (scrape, save, inject dashboard, backup, git push)
    python scraper.py --selftest   # fetch 1 page per source, print classification stats, save nothing
    python scraper.py --discover   # print kakaku filter parameters (pdf_Spec***) found on the SSD listing page
    python scraper.py --no-push    # normal run but skip OneDrive backup / git push
"""

import argparse
import json
import logging
import os
import re
import shutil
import smtplib
import statistics
import subprocess
import time
import traceback
import unicodedata
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup


# ── Paths / mail ──────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
DATA_FILE = BASE_DIR / "data" / "prices.json"
LOG_FILE  = BASE_DIR / "logs" / "scraper.log"
BACKUP_DIR = Path(r"C:\Users\Weilin\OneDrive\ssd-tracker-backup")

EMAIL_TO   = "weilahm@gmail.com"
EMAIL_FROM = "weilahm@gmail.com"
# Generate at https://myaccount.google.com/apppasswords (2FA required)
EMAIL_APP_PASSWORD = os.environ.get("SSD_TRACKER_EMAIL_PASSWORD", "YOUR_GMAIL_APP_PASSWORD")

# ── Alert thresholds ──────────────────────────────────────────────────────────
ALERT_PCT     = 0.05   # change vs previous run   (+-5%)  -> "significant"
ALERT_PCT_7D  = 0.10   # change vs ~7 days ago    (+-10%) -> "significant"

# ── Data retention ────────────────────────────────────────────────────────────
DETAIL_HISTORY_DAYS = 30   # keep per-model lists for this many days; older entries keep summary only
EVENT_KEEP_DAYS     = 90

# ── Scraping behaviour ────────────────────────────────────────────────────────
MAX_PAGES        = 12     # listing pages per source (kakaku shows ~40 items/page; Gen4 2280 has ~250+)
REQUEST_INTERVAL = 1.0    # seconds between requests

# ── Authorized retailers (same whitelist as GPU tracker) ─────────────────────
AUTHORIZED_RETAILERS = [
    "TSUKUMO", "ツクモ",
    "パソコン工房", "PCワンズ",
    "ソフマップ", "ソフマップ.com",
    "Amazon.co.jp", "Amazon",
    "コジマネット", "コジマ",
    "ビックカメラ.com", "ビックカメラ",
    "ヨドバシ.com", "ヨドバシ",
    "ヤマダウェブコム", "ヤマダ電機",
    "ドスパラ", "Dospara",
    "Joshin", "ジョーシン",
    "ノジマ",
    "エディオン",
    "ケーズデンキ",
]


def is_authorized_retailer(shop_name: str) -> bool:
    return any(r in shop_name for r in AUTHORIZED_RETAILERS)


# ── Brands (consumer) ─────────────────────────────────────────────────────────
# canonical display name -> prefixes that kakaku product names start with
CONSUMER_BRANDS = {
    "Samsung":       ["Samsung", "サムスン"],
    "WD":            ["Western Digital", "WD_BLACK", "WD_Black", "WD Blue", "WD", "ウエスタンデジタル"],
    "Crucial":       ["Crucial", "クルーシャル"],
    "Kioxia":        ["KIOXIA", "Kioxia", "キオクシア"],
    "Lexar":         ["Lexar", "レキサー"],
    "Team":          ["TEAM", "Team", "チーム"],
    "Silicon Power": ["Silicon Power", "SiliconPower", "シリコンパワー"],
    "MSI":           ["MSI"],
    "SanDisk":       ["SanDisk", "Sandisk", "サンディスク"],
}

# Enterprise: broader list (not specified by user - edit freely)
ENTERPRISE_BRANDS = {
    **CONSUMER_BRANDS,
    "Micron":   ["Micron", "マイクロン"],
    "Solidigm": ["Solidigm", "ソリダイム"],
    "Intel":    ["Intel", "インテル"],
    "Seagate":  ["Seagate", "シーゲイト"],
    "Kingston": ["Kingston", "キングストン"],
}
MSI_NAME = "MSI"

# ── Capacity buckets (nominal GB) ────────────────────────────────────────────
# bucket: (label, nominal_gb, accept_min_gb, accept_max_gb)
SATA_BUCKETS = [
    ("240G", 240, 235, 245),
    ("480G", 480, 470, 490),
    ("960G", 960, 940, 980),
]
M2_BUCKETS = [
    ("500G", 500, 480, 520),     # 500 / 512
    ("1TB", 1000, 960, 1050),    # 1000 / 1024
    ("2TB", 2000, 1900, 2100),   # 2000 / 2048
    ("4TB", 4000, 3900, 4200),   # 4000 / 4096
]

# ── Exclusions ────────────────────────────────────────────────────────────────
EXCLUDE_EXTERNAL = ["ポータブル", "外付", "PORTABLE", "EXTERNAL", "USB 3.2", "USB3", "USB-C"]
# PS5 models are NOT excluded - only labelled (see is_ps5)
EXCLUDE_PS5      = ["PS5", "PLAYSTATION", "プレイステーション"]
RE_M2_OTHER      = re.compile(r"(?<!\d)22(?:30|42|60|110)(?!\d)")   # non-2280 M.2 lengths
HEATSINK_YES = ["ヒートシンク", "ヒートスプレッダ", "HEATSINK", "HEAT SINK", "HEATSPREADER",
                "HEAT SPREADER", "放熱", "FROZR", "冷却"]
HEATSINK_NO  = re.compile(r"(ヒートシンク|HEATSINK|HEAT SINK)\s*(なし|無し|無)|W/O\s*HEAT|WITHOUT\s*HEAT|NO\s*HEAT")
# manual override by kakaku item id, e.g. {"K0001234567": True}
HEATSINK_OVERRIDE: dict[str, bool] = {}
# model-code patterns that mean "with heatsink" even when the name has no keyword (extend as you find more)
HEATSINK_NAME_PATTERNS = [
    re.compile(r"WDS\d+T\dXH[EM]"),      # WD_BLACK SN850X / SN8100 heatsink SKUs (…XHE / …XHM)
    re.compile(r"CT\d+[TP]\d{3}SSD5"),    # Crucial T500/T700/T705/T710/P510: ...SSD5 = heatsink (SSD3/SSD8 = bare)
    # MSI example (add the models that ship with a heatsink): re.compile(r"SPATIUM M\d+ PRO FROZR"),
]

# ── Listing sources (consumer) ───────────────────────────────────────────────
# kakaku filter ids (confirmed with `--discover`):
#   pdf_Spec102: 8 = M.2 Type2280 / 2 = 2.5inch      pdf_Spec101: 11 = SATA 6Gb/s / 15 = PCIe Gen4 / 17 = PCIe Gen5
#   pdf_Spec003=1 : PS5対応 flag (used below to exclude PS5 models by item id)
# (USB / portable drives are excluded automatically because their interface is USB.)
# Note: "Gen4/Gen5" (pdf_Spec101=18, ~6 items) is intentionally not fetched.
KAKAKU_LIST = "https://kakaku.com/pc/ssd/itemlist.aspx"
# One listing source per (interface, capacity): interface and capacity come from the URL filter itself,
# so we do not depend on parsing the (label-less) spec cells of the listing rows.
SATA_CAPS_GB = [240, 480, 960]
M2_CAPS_GB   = [500, 512, 1000, 2000, 4000]      # 512 -> 500G bucket, nominal 1000/2000/4000 as shown by kakaku
LISTING_SOURCES = (
    [{"id": f"SATA-{c}", "iface": "SATA", "cap_gb": c,
      "url": f"{KAKAKU_LIST}?pdf_Spec101=11&pdf_Spec102=2&pdf_Spec301={c}"} for c in SATA_CAPS_GB]
    + [{"id": f"{g}-{c}", "iface": g, "cap_gb": c,
        "url": f"{KAKAKU_LIST}?pdf_Spec101={sid}&pdf_Spec102=8&pdf_Spec301={c}"}
       for g, sid in (("Gen4", 15), ("Gen5", 17)) for c in M2_CAPS_GB]
)
PS5_SOURCES = [
    f"{KAKAKU_LIST}?pdf_Spec003=1&pdf_Spec101=15&pdf_Spec102=8",
    f"{KAKAKU_LIST}?pdf_Spec003=1&pdf_Spec101=17&pdf_Spec102=8",
]

# ── Enterprise ───────────────────────────────────────────────────────────────
# Keyword search is disabled by default (kakaku.com/search_results/ returned 0 hits in testing).
ENTERPRISE_SEARCH_KEYWORDS_UNUSED = [
    "PM9A3", "PM9D3", "PM1733", "PM1735", "PM1743", "PM893", "PM883",
    "CD6 SSD", "CD7 SSD", "CD8 SSD", "CM6 SSD", "CM7 SSD",
    "Micron 7450", "Micron 7500", "Micron 5400 PRO",
    "Solidigm D7", "Solidigm D5", "Solidigm D3",
    "Ultrastar DC SN", "エンタープライズ SSD", "データセンター SSD", "U.2 SSD", "U.3 SSD", "E1.S SSD",
]
ENTERPRISE_SEARCH_KEYWORDS: list[str] = []
ENTERPRISE_PAGES_PER_KEYWORD = 1
# Enterprise candidates come from kakaku listings: 2.5-inch NVMe (= U.2/U.3) and large SATA drives.
ENT_LISTING_SOURCES = (
    [{"id": f"ENT-nvme-{sid}", "ent_form": "U.2 / U.3", "accept_all": True,
      "url": f"{KAKAKU_LIST}?pdf_Spec101={sid}&pdf_Spec102=2"} for sid in (8, 15)]
    + [{"id": f"ENT-sata-{c}", "ent_form": "SATA 2.5インチ", "accept_all": False, "cap_gb": c,
        "url": f"{KAKAKU_LIST}?pdf_Spec101=11&pdf_Spec102=2&pdf_Spec301={c}"} for c in (1920, 3840, 7680)]
)
ENT_MAX_PAGES = 5
ENT_EXCLUDE = re.compile(r"750 SERIES|OPTANE ?(900|905)")   # consumer-grade models that show up in 2.5" NVMe listings
ENT_PATTERN = re.compile(
    r"(?<![A-Z0-9])(PM9A3|PM9D3|PM1733A?|PM1735|PM1743|PM893|PM883|PM897|CD[5678]|CM[5678]|KCD\w*|KCM\w*|KPM\w*|"
    r"7450|7500|7400|9400|5400 ?PRO|5300 ?PRO|D7-P\w+|D7-PS\w+|D5-P\w+|D3-S\w+|ULTRASTAR|DC SN\d+|"
    r"SSDPE2\w+|SSDPF2\w+|MZQL\w+|MZWL[JR]\w+|MZ7L\w+)(?![A-Z0-9])"
    r"|ENTERPRISE|エンタープライズ|データセンター|DATA ?CENTER|U\.2|U\.3|SFF-8639|E1\.S|E3\.S|22110"
)
ENT_FORMS = [
    ("U.2 / U.3",     re.compile(r"U\.2|U\.3|SFF-8639")),
    ("M.2 22110",     re.compile(r"22110")),
    ("E1.S / E3.S",   re.compile(r"E1\.S|E3\.S|E1\.L|E3\.L")),
    ("SATA 2.5インチ", re.compile(r"SATA")),
]
ENT_REQUIRE_AUTHORIZED = False   # False: prefer authorized shops, fall back to cheapest shop (flagged)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("ssd")


# ══════════════════════════════════════════════════════════════════════════════
# Segment definitions
# ══════════════════════════════════════════════════════════════════════════════
def seg_key(group: str, cap_label: str, heatsink: bool) -> str:
    return f"{group} {cap_label}" + (" HS" if heatsink else "")


def build_consumer_segments() -> dict[str, dict]:
    segs, order = {}, 0
    for cap_label, nominal, *_ in SATA_BUCKETS:
        order += 1
        segs[seg_key("SATA", cap_label, False)] = {
            "kind": "consumer", "group": "SATA", "cap_label": cap_label, "cap_gb": nominal,
            "heatsink": False, "label": f"SATA {cap_label}", "sort": order}
    for group in ("Gen4", "Gen5"):
        for cap_label, nominal, *_ in M2_BUCKETS:
            for hs in (False, True):
                order += 1
                segs[seg_key(group, cap_label, hs)] = {
                    "kind": "consumer", "group": group, "cap_label": cap_label, "cap_gb": nominal,
                    "heatsink": hs, "sort": order,
                    "label": f"M.2 {group} {cap_label}" + "（ヒートシンク付）" * hs}
    return segs


def ent_seg_key(form: str) -> str:
    return f"ENT {form}"


def build_enterprise_segments() -> dict[str, dict]:
    segs = {}
    forms = [f for f, _ in ENT_FORMS] + ["その他"]
    for i, form in enumerate(forms):
        segs[ent_seg_key(form)] = {"kind": "enterprise", "group": "ENT", "form": form,
                                   "label": f"エンタープライズ {form}", "sort": 1000 + i}
    return segs


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════
def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def get(url: str, **kwargs) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
    resp.raise_for_status()
    return resp


def clean_price(text: str) -> int | None:
    nums = re.sub(r"[^\d]", "", text or "")
    return int(nums) if nums else None


def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    """Atomic write (avoids half-written file when OneDrive / Pages sync is touching the folder)."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


def detect_brand(name: str, brands: dict) -> tuple[str | None, str]:
    """Return (canonical brand, name without brand prefix)."""
    n = norm(name).strip()
    pairs = sorted(((a, b) for b, al in brands.items() for a in al), key=lambda x: -len(x[0]))
    for alias, brand in pairs:
        if n.lower().startswith(alias.lower()):
            rest = n[len(alias):].lstrip(" _-　")
            if alias == "WD" and rest[:5].upper() == "BLACK":   # "WD_BLACK" -> alias "WD" + "_BLACK"
                rest = "BLACK" + rest[5:]
            if brand == "SanDisk" and re.match(r"WD[_ ]", rest, re.I):   # kakaku files WD_BLACK/WD Blue under Sandisk
                brand = "WD"
            return brand, rest
    return None, n


# ── Listing parser ───────────────────────────────────────────────────────────
RE_ITEM_ID = re.compile(r"/item/(K\d{10})")


def parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, dict] = {}
    for a in soup.select("a[href*='/item/K']"):
        m = RE_ITEM_ID.search(a.get("href", ""))
        if not m:
            continue
        iid = m.group(1)
        url = f"https://kakaku.com/item/{iid}/"
        text = a.get_text(strip=True)
        it = items.setdefault(iid, {"id": iid, "url": url, "name": "", "price": None,
                                    "spec": "", "release_date": ""})
        if text.startswith(("¥", "￥")):
            p = clean_price(text)
            if p and p > 1000:
                it["price"] = p
        elif len(text) > 8 and not it["name"]:
            it["name"] = text
        if not it["spec"]:
            row = a.find_parent("tr")
            if row is not None:
                ids = {RE_ITEM_ID.search(x.get("href", "")).group(1)
                       for x in row.select("a[href*='/item/K']") if RE_ITEM_ID.search(x.get("href", ""))}
                if len(ids) == 1:
                    it["spec"] = row.get_text(" ", strip=True)
                    td = row.select_one("td.swdate1")
                    if td:
                        parts = td.get_text(strip=True).replace("　", "").split("/")
                        if len(parts) == 3 and all(p.isdigit() for p in parts):
                            it["release_date"] = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return [i for i in items.values() if i["name"]]


def fetch_listing(url: str, max_pages: int = MAX_PAGES) -> list[dict]:
    seen: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        u = url if page == 1 else f"{url}{'&' if '?' in url else '?'}pdf_pg={page}"
        try:
            resp = get(u)
            resp.encoding = resp.apparent_encoding
            found = parse_listing(resp.text)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                log.debug(f"listing end/no such filter [{u}]")      # kakaku returns 404 past the last page
            else:
                log.warning(f"listing fetch failed [{u}]: {e}")
            break
        except Exception as e:
            log.warning(f"listing fetch failed [{u}]: {e}")
            break
        new = [i for i in found if i["id"] not in seen]
        for i in new:
            seen[i["id"]] = i
        time.sleep(REQUEST_INTERVAL)
        if not new:
            break
    return list(seen.values())


def fetch_source(src: dict, max_pages: int = MAX_PAGES) -> list[dict]:
    """fetch_listing + tag each item with what the source filter tells us (interface / capacity / form)."""
    items = fetch_listing(src["url"], max_pages)
    for it in items:
        if src.get("iface"):
            it["iface"] = src["iface"]
        if src.get("cap_gb"):
            it["cap_gb"] = src["cap_gb"]
        if src.get("ent_form"):
            it["ent_form"], it["ent_accept_all"] = src["ent_form"], src.get("accept_all", False)
    return items


# ── Classification ───────────────────────────────────────────────────────────
RE_CAP_LABELED = re.compile(r"容量\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(TB|GB)", re.I)
RE_CAP_ANY     = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(TB|GB)(?![A-Za-z])", re.I)
RE_GEN         = re.compile(r"(?:GEN\s?|PCIE\s?)([345])(?:\.0)?")


def parse_capacity_gb(name: str, spec: str) -> float | None:
    for text, rx in ((spec, RE_CAP_LABELED), (name, RE_CAP_ANY), (spec, RE_CAP_ANY)):
        m = rx.search(norm(text))
        if m:
            v = float(m.group(1))
            return v * 1000 if m.group(2).upper() == "TB" else v
    return None


def has_heatsink(name: str, spec: str, item_id: str = "") -> bool:
    if item_id in HEATSINK_OVERRIDE:
        return HEATSINK_OVERRIDE[item_id]
    t = norm(f"{name} {spec}").upper()
    if HEATSINK_NO.search(t):
        return False
    if any(rx.search(t) for rx in HEATSINK_NAME_PATTERNS):
        return True
    return any(k.upper() in t for k in HEATSINK_YES)


def is_ps5(item: dict, ps5_ids: set) -> bool:
    """kakaku's 'PS5対応' flag, or PS5 mentioned in the product name."""
    t = norm(item["name"]).upper()
    return item["id"] in ps5_ids or any(k.upper() in t for k in EXCLUDE_PS5)


def bucket_for(cap_gb: float, buckets) -> tuple[str, int] | None:
    for label, nominal, lo, hi in buckets:
        if lo <= cap_gb <= hi:
            return label, nominal
    return None


def classify_consumer(item: dict) -> tuple[str | None, str]:
    """Return (segment key, '') or (None, reject_reason)."""
    name, spec = item["name"], item.get("spec", "")
    t = norm(f"{name} {spec}").upper()
    t = t.replace("PCI-EXPRESS", "PCIE").replace("PCI EXPRESS", "PCIE").replace("PCI-E", "PCIE")
    if any(k.upper() in t for k in EXCLUDE_EXTERNAL):
        return None, "external"
    is_m2 = "M.2" in t
    is_25 = "2.5" in t
    # M.2 is limited to Type2280 (2230 / 2242 / 2260 / 22110 are out of scope)
    if is_m2 and RE_M2_OTHER.search(t):
        return None, "not-2280"
    if item.get("iface"):                       # interface + capacity are known from the listing filter
        group = item["iface"]
        buckets = SATA_BUCKETS if group == "SATA" else M2_BUCKETS
    elif re.search(r"PCIE|NVME", t):
        m = RE_GEN.search(t)
        if not m:
            return None, "gen-unknown"
        gen = m.group(1)
        if gen == "3":
            return None, "gen3"
        if not is_m2:
            return None, "not-m2"
        group, buckets = f"Gen{gen}", M2_BUCKETS
    elif "SATA" in t:
        if is_m2 and not is_25:
            return None, "m2-sata"
        group, buckets = "SATA", SATA_BUCKETS
    else:
        return None, "iface-unknown"
    cap = item.get("cap_gb") or parse_capacity_gb(name, spec)
    if cap is None:
        return None, "cap-unknown"
    b = bucket_for(cap, buckets)
    if not b:
        return None, "cap-out-of-scope"
    hs = group != "SATA" and has_heatsink(name, spec, item["id"])
    return seg_key(group, b[0], hs), ""


def classify_enterprise(item: dict) -> tuple[str | None, str]:
    t = norm(f"{item['name']} {item.get('spec', '')}").upper()
    if any(k.upper() in t for k in EXCLUDE_EXTERNAL):
        return None, "external"
    if ENT_EXCLUDE.search(t):
        return None, "consumer-grade"
    if not item.get("ent_accept_all") and not ENT_PATTERN.search(t):
        return None, "not-enterprise"
    if item.get("ent_form"):
        return ent_seg_key(item["ent_form"]), ""
    for form, rx in ENT_FORMS:
        if rx.search(t):
            return ent_seg_key(form), ""
    return ent_seg_key("その他"), ""


def parse_endurance(name: str, spec: str) -> dict:
    t = norm(f"{name} {spec}")
    out = {}
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*TBW", t, re.I)
    if m:
        out["tbw"] = float(m.group(1).replace(",", ""))
    m = re.search(r"(\d+(?:\.\d+)?)\s*DWPD", t, re.I)
    if m:
        out["dwpd"] = float(m.group(1))
    return out


# ── Detail page: authorized-retailer price ───────────────────────────────────
def fetch_prices(item_url: str) -> dict:
    """Return {'auth': (price, shop) | None, 'any': (price, shop) | None}."""
    out = {"auth": None, "any": None}
    for attempt in (1, 2):
        try:
            resp = get(item_url)
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "html.parser")
            for li in soup.select("li.p-priceList_item"):
                shop_el = li.select_one(".p-priceList_shopName")
                price_el = li.select_one(".p-priceList_priceMain")
                if not shop_el or not price_el:
                    continue
                shop = shop_el.get_text(strip=True)
                price = clean_price(price_el.get_text())
                if not price or price < 1000:
                    continue
                if out["any"] is None or price < out["any"][0]:
                    out["any"] = (price, shop)
                if is_authorized_retailer(shop) and (out["auth"] is None or price < out["auth"][0]):
                    out["auth"] = (price, shop)
            cm = re.search(r"(?<![ァ-ヶー])容量\D{0,8}?(\d[\d,.]*)\s*(TB|GB)", norm(soup.get_text(" ", strip=True)))
            if cm:
                v = float(cm.group(1).replace(",", "")) * (1000 if cm.group(2).upper() == "TB" else 1)
                if 100 <= v <= 100000:
                    out["cap_gb"] = v            # fallback capacity for items whose listing row has none
            return out
        except Exception as e:
            if attempt == 2:
                log.warning(f"fetch_prices [{item_url}]: {e}")
            time.sleep(2)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Scrape
# ══════════════════════════════════════════════════════════════════════════════
def to_model(item: dict, brand: str, brands_rest: str, price: int, shop: str, cap_gb: float,
             heatsink: bool, unauthorized: bool = False) -> dict:
    return {
        "maker": brand,
        "name": brands_rest[:100],
        "price": price,
        "price_per_gb": round(price / cap_gb, 2),
        "capacity_gb": cap_gb,
        "heatsink": heatsink,
        "url": item["url"],
        "source": shop,
        "release_date": item.get("release_date", ""),
        **({"unauthorized": True} if unauthorized else {}),
    }


def scrape_consumer() -> dict[str, list[dict]]:
    candidates: dict[str, dict] = {}
    for src in LISTING_SOURCES:
        items = fetch_source(src)
        log.info(f"listing [{src['id']}]: {len(items)} items")
        for it in items:
            candidates.setdefault(it["id"], it)

    ps5_ids: set[str] = set()
    for url in PS5_SOURCES:
        ps5_ids.update(i["id"] for i in fetch_listing(url))
    log.info(f"PS5-flagged items (labelled, not excluded): {len(ps5_ids)}")

    rejects: dict[str, int] = {}
    todo = []
    for it in candidates.values():
        brand, rest = detect_brand(it["name"], CONSUMER_BRANDS)
        if not brand:
            rejects["brand"] = rejects.get("brand", 0) + 1
            continue
        key, why = classify_consumer(it)
        if not key:
            rejects[why] = rejects.get(why, 0) + 1
            continue
        todo.append((it, brand, rest, key))
    log.info(f"consumer candidates after filter: {len(todo)}  rejects: {rejects}")

    result: dict[str, list[dict]] = {}
    for i, (it, brand, rest, key) in enumerate(todo, 1):
        p = fetch_prices(it["url"])
        time.sleep(REQUEST_INTERVAL)
        if p["auth"]:
            price, shop = p["auth"]
            cap = it.get("cap_gb") or parse_capacity_gb(it["name"], it["spec"])
            m = to_model(it, brand, rest, price, shop, cap, SEG_META[key]["heatsink"])
            if is_ps5(it, ps5_ids):
                m["ps5"] = True                 # label only - PS5 models stay in the ranking
            result.setdefault(key, []).append(m)
        if i % 10 == 0:
            log.info(f"    ...{i}/{len(todo)} processed")
    for key in result:
        result[key].sort(key=lambda m: m["price"])
    return result


def scrape_enterprise() -> dict[str, list[dict]]:
    candidates: dict[str, dict] = {}
    for src in ENT_LISTING_SOURCES:
        for it in fetch_source(src, ENT_MAX_PAGES):
            candidates.setdefault(it["id"], it)
    for kw in ENTERPRISE_SEARCH_KEYWORDS:
        url = f"https://kakaku.com/search_results/{quote(kw)}/"
        for it in fetch_listing(url, ENTERPRISE_PAGES_PER_KEYWORD):
            candidates.setdefault(it["id"], it)
    log.info(f"enterprise search hits: {len(candidates)}")

    todo = []
    for it in candidates.values():
        brand, rest = detect_brand(it["name"], ENTERPRISE_BRANDS)
        if not brand:
            continue
        key, _ = classify_enterprise(it)
        if key:
            todo.append((it, brand, rest, key))
    log.info(f"enterprise candidates after filter: {len(todo)}")

    result: dict[str, list[dict]] = {}
    for it, brand, rest, key in todo:
        p = fetch_prices(it["url"])
        time.sleep(REQUEST_INTERVAL)
        pick, unauth = (p["auth"], False) if p["auth"] else (p["any"], True)
        if not pick or (unauth and ENT_REQUIRE_AUTHORIZED):
            continue
        price, shop = pick
        cap = it.get("cap_gb") or parse_capacity_gb(it["name"], it["spec"]) or p.get("cap_gb")
        if not cap:
            log.warning(f"enterprise: capacity unknown, skipped: {it['name'][:60]}")
            continue
        m = to_model(it, brand, rest, price, shop, cap, False, unauthorized=unauth)
        m["price_per_tb"] = round(price / (cap / 1000))
        m["form"] = SEG_META[key]["form"]
        m.update(parse_endurance(it["name"], it["spec"]))
        result.setdefault(key, []).append(m)
    for key in result:
        result[key].sort(key=lambda m: m["price_per_tb"])
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Summaries / events
# ══════════════════════════════════════════════════════════════════════════════
def summarize(models: list[dict], enterprise: bool) -> dict:
    if not models:
        return {"min_price": None, "median_price": None, "min_per_gb": None,
                "median_per_gb": None, "n_models": 0}
    prices = [m["price"] for m in models]
    pgb = [m["price_per_gb"] for m in models]
    s = {"min_price": min(prices), "median_price": int(statistics.median(prices)),
         "min_per_gb": round(min(pgb), 2), "median_per_gb": round(statistics.median(pgb), 2),
         "n_models": len(models)}
    if enterprise:
        s["min_per_tb"] = min(m["price_per_tb"] for m in models)
    return s


def metric_of(entry: dict, enterprise: bool):
    return entry.get("min_per_tb") if enterprise else entry.get("min_price")


def detect_events(seg_data: dict, seg_name: str, new_entry: dict, now: datetime) -> list[dict]:
    meta = seg_data["meta"]
    ent = meta["kind"] == "enterprise"
    unit = "/TB" if ent else ""
    today = now.strftime("%Y-%m-%d %H:%M")
    cur = metric_of(new_entry, ent)
    if cur is None:
        return []
    history = [h for h in seg_data["history"] if metric_of(h, ent)]
    label = meta["label"]
    if not history:
        return [{"type": "new_model", "date": today, "segment": seg_name, "current": cur, "unit": unit,
                 "label": f"NEW {label} 初検出 (¥{cur:,}{unit})"}]
    events = []
    prev = metric_of(history[-1], ent)
    chg = (cur - prev) / prev
    if abs(chg) >= ALERT_PCT:
        events.append({"type": "price_change", "date": today, "segment": seg_name, "prev": prev,
                       "current": cur, "unit": unit, "change": f"{chg:+.1%}",
                       "label": f"{'値上がり' if chg > 0 else '値下がり'} {label} {chg:+.1%} "
                                f"¥{prev:,} -> ¥{cur:,}{unit}"})
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    old = [h for h in history if h["date"] <= cutoff]
    if old:
        ref = metric_of(old[-1], ent)
        chg7 = (cur - ref) / ref
        already = any(e["type"] == "price_change_7d" and e["date"][:10] == today[:10]
                      for e in seg_data.get("events", []))
        if abs(chg7) >= ALERT_PCT_7D and not already:
            events.append({"type": "price_change_7d", "date": today, "segment": seg_name, "prev": ref,
                           "current": cur, "unit": unit, "change": f"{chg7:+.1%}",
                           "label": f"7日 {'上昇' if chg7 > 0 else '下落'} {label} {chg7:+.1%} "
                                    f"¥{ref:,} -> ¥{cur:,}{unit}"})
    return events


def prune(seg_data: dict, now: datetime):
    detail_cut = (now - timedelta(days=DETAIL_HISTORY_DAYS)).strftime("%Y-%m-%d %H:%M")
    for h in seg_data["history"]:
        if h["date"] < detail_cut and "models" in h:
            del h["models"]
    ev_cut = (now - timedelta(days=EVENT_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M")
    seg_data["events"] = [e for e in seg_data.get("events", []) if e["date"] >= ev_cut]


# ── Mail ──────────────────────────────────────────────────────────────────────
def send_email(subject: str, body_html: str):
    if EMAIL_APP_PASSWORD == "YOUR_GMAIL_APP_PASSWORD":
        log.warning("Email not configured - skipping notification.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_FROM, EMAIL_TO
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info(f"Email sent -> {EMAIL_TO}")
    except Exception as e:
        log.error(f"Email failed: {e}")


def build_email_html(events: list[dict], today: str) -> str:
    rows = ""
    for ev in events:
        icon = "🆕" if ev["type"] == "new_model" else ("📈" if ev.get("current", 0) > ev.get("prev", 0) else "📉")
        rows += f"<tr><td>{icon}</td><td>{ev['label']}</td><td>{ev['date']}</td></tr>"
    return f"""
<html><body style="font-family:sans-serif;background:#f5f6f8;color:#1a1d23;padding:24px">
<h2 style="color:#2563eb">SSD Price Tracker - {today} アップデート</h2>
<table border="0" cellpadding="8" style="border-collapse:collapse;width:100%;background:#fff">
<thead><tr style="background:#eef0f3;color:#6b7280;font-size:12px"><th></th><th align="left">内容</th><th align="left">日付</th></tr></thead>
<tbody>{rows}</tbody></table>
<p style="color:#6b7280;font-size:12px;margin-top:24px">SSD Price Tracker - 自動送信メール</p>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# Main run
# ══════════════════════════════════════════════════════════════════════════════
SEG_META = {**build_consumer_segments(), **build_enterprise_segments()}


def run(push: bool = True):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d %H:%M")
    data = load_data()
    first_run = not any(v.get("history") for v in data.values())

    scraped = scrape_consumer()
    scraped.update(scrape_enterprise())

    all_events = []
    for key, meta in SEG_META.items():
        models = scraped.get(key, [])
        ent = meta["kind"] == "enterprise"
        seg = data.setdefault(key, {"meta": meta, "history": [], "events": []})
        seg["meta"] = meta                       # config changes propagate
        if not models and not seg["history"]:
            if ent:
                data.pop(key, None)              # enterprise: don't store never-seen empty shells
            continue                             # consumer: shell kept so the matrix grid stays complete
        entry = {"date": today, **summarize(models, ent), "models": models}
        events = detect_events(seg, key, entry, now) if models else []
        if first_run:                            # initial population: don't flood Pick Up with "NEW" for every segment
            events = [e for e in events if e["type"] != "new_model"]
        all_events.extend(events)
        hist = seg["history"]
        idx = next((i for i, e in enumerate(hist) if e["date"] == today), None)
        if idx is None:
            hist.append(entry)
        else:
            hist[idx] = entry
        seg["events"].extend(events)
        prune(seg, now)

    save_data(data)
    n_models = sum(len(v) for v in scraped.values())
    log.info(f"Data saved. {n_models} models, {len(all_events)} event(s).")

    try:
        from inject_data import inject
        inject()
    except Exception as e:
        log.warning(f"dashboard inject failed: {e}")

    if all_events:
        send_email(f"[SSD Tracker] {today} - {len(all_events)}件の変動を検出",
                   build_email_html(all_events, today))
    else:
        log.info("No significant events - no email sent.")

    if not push:
        return

    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DATA_FILE, BACKUP_DIR / "prices.json")
        shutil.copy2(BASE_DIR / "dashboard.html", BACKUP_DIR / "dashboard.html")
        log.info("Files backed up to OneDrive.")
    except Exception as e:
        log.warning(f"OneDrive backup failed: {e}")

    try:
        subprocess.run(["git", "add", "data/prices.json", "dashboard.html"], cwd=BASE_DIR, check=True)
        st = subprocess.run(["git", "status", "--porcelain"], cwd=BASE_DIR, capture_output=True, text=True)
        if not st.stdout.strip():
            log.info("git: no changes to commit.")
            return
        subprocess.run(["git", "commit", "-m", f"auto update {today}"], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
        log.info("GitHub Pages updated successfully.")
    except Exception as e:
        log.warning(f"GitHub push failed: {e}")


# ── Diagnostics ──────────────────────────────────────────────────────────────
def selftest():
    """Fetch page 1 of a few sources and show how items would be classified. Saves nothing."""
    ps5_ids = set()
    for url in PS5_SOURCES:
        ps5_ids.update(i["id"] for i in fetch_listing(url, max_pages=1))
    print(f"(PS5-flagged items on page 1 of PS5 sources: {len(ps5_ids)})")
    for src in LISTING_SOURCES:
        if src["cap_gb"] not in (480, 1000):
            continue
        items = fetch_source(src, max_pages=1)
        print(f"\n== {src['id']}: {len(items)} items  {src['url']}")
        if items:
            print("   row text sample:", items[0]["spec"][:160].replace("\n", " "))
        counts = {}
        for it in items:
            brand, rest = detect_brand(it["name"], CONSUMER_BRANDS)
            key, why = classify_consumer(it) if brand else (None, "brand")
            tag = key or why
            counts[tag] = counts.get(tag, 0) + 1
            if brand:
                print(f"  {brand:13} {tag:14}{' PS5' if is_ps5(it, ps5_ids) else '    '} {rest[:62]}")
        print("  summary:", counts)
    src = next(x for x in LISTING_SOURCES if x["id"] == "Gen4-1000")
    p1 = {i["id"] for i in fetch_listing(src["url"], max_pages=1)}
    try:
        r2 = get(src["url"] + "&pdf_pg=2"); r2.encoding = r2.apparent_encoding
        p2 = {i["id"] for i in parse_listing(r2.text)}
    except Exception as e:
        p2 = set(); print("page 2 fetch failed:", e)
    print(f"\n== pagination check (Gen4-1000): page1={len(p1)} page2={len(p2)} new_on_page2={len(p2 - p1)}"
          f"  -> {'OK' if p2 - p1 else 'pdf_pg did NOT give new items (tell me!)'}")
    for src in ENT_LISTING_SOURCES[:4]:
        items = fetch_source(src, max_pages=1)
        print(f"\n== {src['id']}: {len(items)} items")
        for it in items[:6]:
            brand, rest = detect_brand(it["name"], ENTERPRISE_BRANDS)
            print(f"  {brand or '-':10} {classify_enterprise(it)[0] or 'x':16} {it['name'][:60]}")


def discover():
    resp = get(KAKAKU_LIST)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")
    seen = set()
    for a in soup.select("a[href*='pdf_Spec']"):
        href, label = a.get("href", ""), a.get_text(strip=True)
        for m in re.finditer(r"(pdf_Spec\d+)=(\d+)", href):
            k = (m.group(1), m.group(2), label)
            if k not in seen:
                seen.add(k)
                print(f"{m.group(1)}={m.group(2):>6}  {label}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    try:
        if args.selftest:
            selftest()
        elif args.discover:
            discover()
        else:
            run(push=not args.no_push)
    except Exception:
        log.error(traceback.format_exc())
