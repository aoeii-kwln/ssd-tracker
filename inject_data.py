"""
inject_data.py - embed prices.json into dashboard.html (via dashboard_template.html)
Called automatically at the end of scraper.py.
"""
import json
from pathlib import Path

BASE_DIR  = Path(__file__).parent
DATA_FILE = BASE_DIR / "data" / "prices.json"
TMPL_FILE = BASE_DIR / "dashboard_template.html"
OUT_FILE  = BASE_DIR / "dashboard.html"


def inject():
    if not DATA_FILE.exists():
        print("[inject] prices.json not found")
        return
    if not TMPL_FILE.exists():
        print("[inject] dashboard_template.html not found")
        return

    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)
    with open(TMPL_FILE, encoding="utf-8") as f:
        html = f.read()

    # "</" inside JSON must not terminate the <script> block
    json_str = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("__PRICES_DATA__", json_str)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    n_seg = sum(1 for v in data.values() if v.get("history"))
    print(f"[inject] dashboard.html updated ({n_seg} segments with data)")


if __name__ == "__main__":
    inject()
