"""
Dealer Website Scanner
======================
Crawls dealer inventory pages, visits each vehicle detail page,
checks price / image / MPG against your rules, and emails a daily report.

Requirements:
    pip install requests beautifulsoup4 anthropic python-dotenv

Setup:
    1. Copy .env.example to .env and fill in your values
    2. Add your sites to sites.json
    3. Run: python scanner.py
    4. Schedule daily: cron, Task Scheduler, or a cloud job
"""

import os
import json
import time
import smtplib
import logging
import re
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import anthropic
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
EMAIL_FROM        = os.getenv("EMAIL_FROM")
EMAIL_PASSWORD    = os.getenv("EMAIL_PASSWORD")
EMAIL_TO          = os.getenv("EMAIL_TO")          # comma-separated
EMAIL_CC          = os.getenv("EMAIL_CC", "")      # optional
SMTP_HOST         = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.getenv("SMTP_PORT", 587))
SITES_FILE        = os.getenv("SITES_FILE", "sites.json")
RULES_FILE        = os.getenv("RULES_FILE", "rules.json")
REQUEST_DELAY     = float(os.getenv("REQUEST_DELAY", 1.5))   # seconds between requests
MAX_VEHICLES      = int(os.getenv("MAX_VEHICLES", 200))       # safety cap per site

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Load config files ─────────────────────────────────────────────────────────

def load_sites():
    if not os.path.exists(SITES_FILE):
        log.error(f"Sites file not found: {SITES_FILE}")
        raise FileNotFoundError(f"Create {SITES_FILE} — see sites.json.example")
    with open(SITES_FILE) as f:
        return json.load(f)

def load_rules():
    defaults = {
        "price": {
            "flag_zero": True,
            "flag_msrp_only": True,
            "flag_contact_dealer": True,
            "contact_keywords": [
                "contact dealer", "call for price", "call us",
                "request price", "get a quote", "inquire", "ask for price"
            ]
        },
        "image": {
            "flag_coming_soon": True,
            "flag_broken": True,
            "coming_soon_keywords": ["coming soon", "image coming", "photo coming"]
        },
        "mpg": {
            "flag_missing": True,
            "flag_zero": True,
            "flag_na": False
        }
    }
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            return json.load(f)
    log.warning(f"{RULES_FILE} not found — using default rules.")
    return defaults

# ── Crawling ──────────────────────────────────────────────────────────────────

def fetch_page(url, timeout=15):
    """Fetch a URL and return (soup, raw_html) or (None, None) on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return soup, resp.text
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None, None

def discover_vehicle_urls(site):
    """
    Crawl an inventory listing page and extract individual vehicle detail URLs.
    Handles pagination automatically.
    """
    inventory_url = site["url"]
    base = f"{urlparse(inventory_url).scheme}://{urlparse(inventory_url).netloc}"
    found_urls = set()
    page_url = inventory_url
    page_num = 1

    while page_url and len(found_urls) < MAX_VEHICLES:
        log.info(f"  Crawling page {page_num}: {page_url}")
        soup, _ = fetch_page(page_url)
        if not soup:
            break

        # Extract vehicle links — common dealer platform patterns
        vehicle_links = extract_vehicle_links(soup, base, site)
        before = len(found_urls)
        found_urls.update(vehicle_links)
        log.info(f"  Found {len(found_urls) - before} new vehicle links (total: {len(found_urls)})")

        # Pagination: find next page link
        page_url = find_next_page(soup, page_url, base)
        if page_url:
            page_num += 1
            time.sleep(REQUEST_DELAY)
        else:
            break

    return list(found_urls)[:MAX_VEHICLES]

def extract_vehicle_links(soup, base_url, site):
    """Extract vehicle detail page URLs from an inventory listing page."""
    urls = set()

    # Custom selector override per site
    selector = site.get("vehicle_link_selector")
    if selector:
        for a in soup.select(selector):
            href = a.get("href", "")
            if href:
                urls.add(urljoin(base_url, href))
        return urls

    # Auto-detection: common dealer platform patterns
    patterns = [
        # URL path patterns common to dealer sites
        r"/inventory/",
        r"/vehicle/",
        r"/new/",
        r"/used/",
        r"/vdp/",           # Vehicle Detail Page
        r"/details/",
        r"/cars/",
        r"\?vin=",
        r"\?stocknum=",
    ]

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)
        # Must be same domain
        if urlparse(full_url).netloc != urlparse(base_url).netloc:
            continue
        for pat in patterns:
            if re.search(pat, full_url, re.IGNORECASE):
                # Exclude pagination and filter links
                if not any(x in full_url for x in ["?page=", "&page=", "/filter", "/search", "#"]):
                    urls.add(full_url)
                break

    return urls

def find_next_page(soup, current_url, base_url):
    """Find the next pagination URL, if any."""
    # Look for rel="next" link
    next_link = soup.find("a", rel="next")
    if next_link and next_link.get("href"):
        return urljoin(base_url, next_link["href"])

    # Look for "Next" button text
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if text in ("next", "next page", ">", "»"):
            href = urljoin(base_url, a["href"])
            if href != current_url:
                return href

    return None

# ── Vehicle page analysis ─────────────────────────────────────────────────────

def extract_page_text(soup):
    """Extract visible text from a vehicle detail page for AI analysis."""
    # Remove script/style/nav noise
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    # Truncate to keep token usage reasonable
    return text[:4000]

def check_images(soup, rules):
    """Check image status directly from HTML."""
    img_rule = rules["image"]
    issues = []
    status = "ok"

    imgs = soup.find_all("img")
    if not imgs:
        if img_rule.get("flag_broken"):
            issues.append("No images found on page")
            status = "error"
        return status, "No images", issues

    coming_soon_kw = img_rule.get("coming_soon_keywords", ["coming soon"])
    for img in imgs:
        alt = (img.get("alt") or "").lower()
        src = (img.get("src") or "").lower()
        for kw in coming_soon_kw:
            if kw in alt or kw in src:
                issues.append(f"Image contains '{kw}'")
                status = "error"

    img_count = len(imgs)
    note = f"{img_count} image{'s' if img_count != 1 else ''}"
    return status, note, issues

def analyze_vehicle_with_ai(vehicle_url, page_text, rules, client):
    """Use Claude to analyze the extracted page text for price, image, MPG issues."""
    keywords = ", ".join(rules["price"].get("contact_keywords", []))

    prompt = f"""You are a dealer website QA checker. Analyze this vehicle detail page content and return ONLY a JSON object.

URL: {vehicle_url}

PAGE CONTENT:
{page_text}

Return this exact JSON structure:
{{
  "vehicle_name": "Year Make Model Trim extracted from page, or 'Unknown'",
  "price_status": "ok" or "warn" or "error",
  "price_note": "actual price found e.g. $32,500, or reason e.g. Contact dealer for price",
  "image_status": "ok" or "warn" or "error",
  "image_note": "brief note e.g. Photos available or Coming soon",
  "mpg_status": "ok" or "warn" or "error",
  "mpg_note": "MPG if found e.g. 28 city / 38 hwy, or Not listed",
  "issues": ["list only actual problems found, empty array if none"]
}}

Alert rules to apply:
- price=error if: price is $0, blank/missing, shows ONLY MSRP with no sale price, or contains these contact-dealer keywords: {keywords}
- price=warn if: price seems unusually high/low but is present
- image=error if: page contains "coming soon" near images, or no vehicle images detected
- mpg=error if: MPG data is completely absent or shows 0
- mpg=warn if: MPG shows as N/A or TBD

Be accurate — only flag real issues found in the actual page content above.
Return ONLY valid JSON with no markdown formatting."""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        log.warning(f"AI analysis failed for {vehicle_url}: {e}")
        return {
            "vehicle_name": "Unknown",
            "price_status": "skip", "price_note": "Analysis error",
            "image_status": "skip", "image_note": "Analysis error",
            "mpg_status": "skip",   "mpg_note": "Analysis error",
            "issues": ["AI analysis failed"]
        }

def scan_vehicle(vehicle_url, rules, client):
    """Fetch a vehicle detail page and run all checks."""
    soup, _ = fetch_page(vehicle_url)
    if not soup:
        return {
            "url": vehicle_url,
            "vehicle_name": "Could not load",
            "price_status": "error", "price_note": "Page unreachable",
            "image_status": "error", "image_note": "Page unreachable",
            "mpg_status":   "error", "mpg_note":   "Page unreachable",
            "issues": ["Page failed to load"]
        }

    # Direct image check from HTML
    img_status, img_note, img_issues = check_images(soup, rules)

    # AI analysis of page text
    page_text = extract_page_text(soup)
    result = analyze_vehicle_with_ai(vehicle_url, page_text, rules, client)

    # Override image result with our direct HTML check if it found an issue
    if img_status == "error" and result.get("image_status") != "error":
        result["image_status"] = "error"
        result["image_note"] = img_note
        result["issues"] = result.get("issues", []) + img_issues

    result["url"] = vehicle_url
    return result

# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan(sites, rules):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    all_results = []
    scan_time = datetime.now()

    for site in sites:
        log.info(f"\n{'='*60}")
        log.info(f"Scanning site: {site['name']} — {site['url']}")
        log.info(f"{'='*60}")

        vehicle_urls = discover_vehicle_urls(site)
        log.info(f"Discovered {len(vehicle_urls)} vehicle pages on {site['name']}")

        site_results = []
        for i, vurl in enumerate(vehicle_urls, 1):
            log.info(f"  [{i}/{len(vehicle_urls)}] Checking: {vurl}")
            result = scan_vehicle(vurl, rules, client)
            result["site_name"] = site["name"]
            result["site_url"]  = site["url"]
            site_results.append(result)
            all_results.append(result)

            status = result.get("price_status","?")
            issues = result.get("issues", [])
            if issues:
                log.info(f"    ⚠ {'; '.join(issues)}")
            else:
                log.info(f"    ✓ OK — price:{status}")

            time.sleep(REQUEST_DELAY)

        log.info(f"\n{site['name']}: {len(site_results)} vehicles scanned.")

    return all_results, scan_time

# ── Report & email ────────────────────────────────────────────────────────────

def build_html_report(results, scan_time):
    alerts   = [r for r in results if any(r.get(f"{k}_status") == "error" for k in ["price","image","mpg"])]
    warnings = [r for r in results if not r in alerts and any(r.get(f"{k}_status") == "warn" for k in ["price","image","mpg"])]
    clear    = [r for r in results if r not in alerts and r not in warnings]

    def badge(status):
        colors = {"ok":"#27500A;background:#EAF3DE", "warn":"#633806;background:#FAEEDA",
                  "error":"#791F1F;background:#FCEBEB", "skip":"#444441;background:#F1EFE8"}
        labels = {"ok":"OK","warn":"Warn","error":"Alert","skip":"—"}
        c = colors.get(status, colors["skip"])
        return f'<span style="color:{c.split(";")[0]};{c.split(";")[1]};padding:2px 8px;border-radius:20px;font-size:11px;font-weight:500">{labels.get(status,"—")}</span>'

    def vehicle_rows(rows):
        html = ""
        for r in rows:
            ovr = "error" if any(r.get(f"{k}_status")=="error" for k in ["price","image","mpg"]) else \
                  "warn"  if any(r.get(f"{k}_status")=="warn"  for k in ["price","image","mpg"]) else "ok"
            html += f"""<tr style="border-bottom:1px solid #eee">
              <td style="padding:8px;font-size:13px;font-weight:500;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{r.get('vehicle_name','')}">
                <a href="{r.get('url','#')}" style="color:inherit;text-decoration:none">{r.get('vehicle_name','Unknown')}</a>
              </td>
              <td style="padding:8px;font-size:12px;color:#666">{r.get('site_name','')}</td>
              <td style="padding:8px;font-size:12px">{r.get('price_note','—')}</td>
              <td style="padding:8px;font-size:12px">{r.get('image_note','—')}</td>
              <td style="padding:8px;font-size:12px">{r.get('mpg_note','—')}</td>
              <td style="padding:8px">{badge(ovr)}</td>
            </tr>"""
        return html

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body{{font-family:Arial,sans-serif;color:#222;max-width:900px;margin:0 auto;padding:20px}}
  h1{{font-size:20px;font-weight:600;margin-bottom:4px}}
  .subtitle{{color:#666;font-size:13px;margin-bottom:24px}}
  .metrics{{display:flex;gap:16px;margin-bottom:24px}}
  .metric{{background:#f5f5f5;border-radius:8px;padding:12px 20px;text-align:center}}
  .metric-val{{font-size:28px;font-weight:600}}
  .metric-lbl{{font-size:12px;color:#666;margin-top:2px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{text-align:left;padding:8px;font-size:11px;color:#888;border-bottom:2px solid #eee;font-weight:600}}
  h2{{font-size:15px;font-weight:600;margin:28px 0 12px;padding-bottom:6px;border-bottom:1px solid #eee}}
</style></head><body>
<h1>Dealer Site Scan Report</h1>
<div class="subtitle">Run on {scan_time.strftime('%B %d, %Y at %I:%M %p')}</div>

<div class="metrics">
  <div class="metric"><div class="metric-val">{len(results)}</div><div class="metric-lbl">Vehicles scanned</div></div>
  <div class="metric"><div class="metric-val" style="color:#A32D2D">{len(alerts)}</div><div class="metric-lbl">Alerts</div></div>
  <div class="metric"><div class="metric-val" style="color:#854F0B">{len(warnings)}</div><div class="metric-lbl">Warnings</div></div>
  <div class="metric"><div class="metric-val" style="color:#3B6D11">{len(clear)}</div><div class="metric-lbl">All clear</div></div>
</div>"""

    if alerts:
        html += "<h2>🚨 Alerts — action required</h2>"
        html += '<table><thead><tr><th>Vehicle</th><th>Site</th><th>Price</th><th>Image</th><th>MPG</th><th>Status</th></tr></thead><tbody>'
        html += vehicle_rows(alerts)
        html += "</tbody></table>"

    if warnings:
        html += "<h2>⚠ Warnings</h2>"
        html += '<table><thead><tr><th>Vehicle</th><th>Site</th><th>Price</th><th>Image</th><th>MPG</th><th>Status</th></tr></thead><tbody>'
        html += vehicle_rows(warnings)
        html += "</tbody></table>"

    if clear:
        html += f"<h2>✓ All clear ({len(clear)} vehicles)</h2>"
        html += '<table><thead><tr><th>Vehicle</th><th>Site</th><th>Price</th><th>Image</th><th>MPG</th><th>Status</th></tr></thead><tbody>'
        html += vehicle_rows(clear)
        html += "</tbody></table>"

    html += "</body></html>"
    return html

def send_email(html_report, results, scan_time):
    alerts = sum(1 for r in results if any(r.get(f"{k}_status")=="error" for k in ["price","image","mpg"]))

    subject = f"Dealer Scan Report — {scan_time.strftime('%b %d')} | {alerts} alert{'s' if alerts!=1 else ''}, {len(results)} vehicles"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    if EMAIL_CC:
        msg["Cc"]  = EMAIL_CC

    msg.attach(MIMEText(html_report, "html"))

    recipients = [e.strip() for e in EMAIL_TO.split(",")]
    if EMAIL_CC:
        recipients += [e.strip() for e in EMAIL_CC.split(",")]

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, recipients, msg.as_string())
        log.info(f"Email report sent to {EMAIL_TO}")
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        # Save report locally as fallback
        report_path = f"report_{scan_time.strftime('%Y%m%d_%H%M')}.html"
        with open(report_path, "w") as f:
            f.write(html_report)
        log.info(f"Report saved locally: {report_path}")

def save_results(results, scan_time):
    path = f"scan_results_{scan_time.strftime('%Y%m%d_%H%M')}.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    log.info(f"Results saved to {path}")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Dealer Site Scanner starting...")

    sites = load_sites()
    rules = load_rules()

    log.info(f"Loaded {len(sites)} site(s) to scan.")

    results, scan_time = run_scan(sites, rules)

    log.info(f"\nScan complete — {len(results)} vehicles checked.")

    html = build_html_report(results, scan_time)
    save_results(results, scan_time)
    send_email(html, results, scan_time)

    log.info("Done.")
