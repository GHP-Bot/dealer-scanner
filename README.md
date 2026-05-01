# Dealer Website Scanner

Automatically crawls your dealer inventory sites daily, checks each vehicle's
price, images, and MPG data against your rules, and emails you a report.

---

## Setup

### 1. Install dependencies

```bash
pip install requests beautifulsoup4 anthropic python-dotenv
```

### 2. Configure your environment

```bash
cp .env.example .env
# Edit .env with your Anthropic API key and email credentials
```

**Getting a Gmail App Password** (if using Gmail):
1. Enable 2-Factor Authentication on your Google account
2. Go to Google Account → Security → App Passwords
3. Create a new app password and paste it into `.env` as `EMAIL_PASSWORD`

**Using Outlook / Office 365:**
```
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
```

**Using SendGrid (recommended for reliability):**
```
SMTP_HOST=smtp.sendgrid.net
SMTP_PORT=587
EMAIL_FROM=noreply@yourdomain.com
EMAIL_PASSWORD=your-sendgrid-api-key
```

### 3. Add your sites

```bash
cp sites.json.example sites.json
# Edit sites.json with your dealer inventory URLs
```

**sites.json fields:**
- `name` — display name for reports
- `url` — the inventory listing page URL (not a single vehicle page)
- `notes` — optional notes shown in logs
- `vehicle_link_selector` — optional CSS selector to find vehicle links (overrides auto-detection)

### 4. Customize your alert rules

Edit `rules.json` to adjust what gets flagged. The defaults cover:
- Price: $0, blank, MSRP-only, contact-dealer phrases
- Image: "Coming soon" text, broken/missing images
- MPG: completely absent or zero

### 5. Test run

```bash
python scanner.py
```

---

## Scheduling — run daily automatically

### Mac / Linux (cron)

Open your crontab:
```bash
crontab -e
```

Add a line to run at 8:00 AM every day:
```
0 8 * * * cd /path/to/dealer_scanner && /usr/bin/python3 scanner.py >> logs/scanner.log 2>&1
```

Make the logs directory first:
```bash
mkdir -p /path/to/dealer_scanner/logs
```

### Windows (Task Scheduler)

1. Open Task Scheduler → Create Basic Task
2. Name: "Dealer Scanner"
3. Trigger: Daily at 8:00 AM
4. Action: Start a program
   - Program: `C:\Python312\python.exe`
   - Arguments: `scanner.py`
   - Start in: `C:\path\to\dealer_scanner`
5. Finish

### Cloud / always-on options

**GitHub Actions (free)** — runs in the cloud on a schedule:

Create `.github/workflows/scan.yml`:
```yaml
name: Daily Dealer Scan
on:
  schedule:
    - cron: '0 13 * * *'  # 8 AM ET = 1 PM UTC
  workflow_dispatch:       # also allows manual trigger

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install requests beautifulsoup4 anthropic python-dotenv
      - run: python scanner.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
          EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
          EMAIL_TO: ${{ secrets.EMAIL_TO }}
```

Store your secrets in GitHub → Settings → Secrets and variables → Actions.

---

## Output files

Each run produces:
- `scan_results_YYYYMMDD_HHMM.json` — full machine-readable results
- `report_YYYYMMDD_HHMM.html` — local HTML report (saved if email fails)
- Email report sent to your configured address

---

## Troubleshooting

**"No vehicle links found"**  
Some dealer sites use JavaScript to load inventory (React/Vue SPAs). The
plain HTTP crawler can't see JS-rendered content. Solution: add the
`vehicle_link_selector` field pointing to a static link, or use a headless
browser like Playwright. Ask for the Playwright version if needed.

**"Page failed to load"**  
The site may be blocking bots. Increase `REQUEST_DELAY` in `.env` to 3-5
seconds, or the site requires a real browser session.

**Email not sending**  
Run with `python scanner.py` — the HTML report is always saved locally
as a fallback even if email fails.
