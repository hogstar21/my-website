#!/usr/bin/env python3
"""
WW3 Tracker - Daily Auto-Update Script
Uses Google Gemini (free tier) + Google News RSS to update claims and rebuild HTML.
Runs via GitHub Actions every hour.
"""

import json
import time
import os
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree

# ── CONFIG ──────────────────────────────────────────────────────────────────
CLAIMS_FILE    = "claims.json"
HTML_OUT_FILE  = "ww3-tracker.html"
IMAGE_PATH     = "images/4chan-prediction.jpg"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL     = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
MAX_HEADLINES  = 5

_now_utc  = datetime.now(timezone.utc)
_now_edt  = _now_utc - timedelta(hours=4)
TODAY     = _now_edt.strftime("%b %d, %Y \u00b7 %-I:%M %p EDT").upper()
TODAY_ISO = _now_utc.strftime("%Y-%m-%d")

# ── LOAD / SAVE ──────────────────────────────────────────────────────────────
def load_claims():
    with open(CLAIMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_claims(data):
    with open(CLAIMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── RSS FETCH ────────────────────────────────────────────────────────────────
def fetch_headlines(keywords):
    headlines = []
    query = " OR ".join(f'"{k}"' for k in keywords[:3])
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read()
        root = ElementTree.fromstring(xml)
        for item in root.findall(".//item")[:MAX_HEADLINES]:
            title = item.findtext("title", "").strip()
            pub   = item.findtext("pubDate", "").strip()
            if title:
                headlines.append(f"{title} [{pub[:16]}]")
    except Exception as e:
        print(f"  RSS error for '{keywords[0]}': {e}")
    return headlines

# ── GEMINI ───────────────────────────────────────────────────────────────────
def ask_gemini(prompt):
    if not GEMINI_API_KEY:
        print("  No GEMINI_API_KEY")
        return ""
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512}
    }).encode()
    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
            return result["candidates"][0]["content"]["parts"][0]["text"].strip()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = (attempt + 1) * 10
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Gemini error: {e}")
                return ""
        except Exception as e:
            print(f"  Gemini error: {e}")
            return ""
    return ""

# ── UPDATE CLAIM ─────────────────────────────────────────────────────────────
def update_claim(claim):
    cid    = claim["id"]
    text   = claim["text"]
    status = claim["status"]

    if status == "no" and any(w in text.lower() for w in
       ["alien", "mj12", "plf", "5 billion", "world government",
        "dome of the rock", "tsar bomb", "civil war", "colorado", "invaded from"]):
        print(f"  [{cid}] Locked NO — skipping")
        return claim

    # Lock confirmed YES claims - never allow downgrade
    if status == "yes":
        print(f"  [{cid}] Locked YES — skipping")
        return claim

    print(f"  [{cid}] Checking: {text[:55]}...")
    headlines = fetch_headlines(claim.get("keywords", [text]))
    if not headlines:
        return claim

    prompt = f"""Fact-checking a 2025 4chan WW3 prediction.
CLAIM: "{text}"
CURRENT STATUS: {status.upper()}
HEADLINES:
{chr(10).join(f"- {h}" for h in headlines)}

Reply ONLY in this JSON (no markdown):
{{"status":"yes|no|partial|watch","has_update":true|false,"update_text":"one sentence under 180 chars or empty","update_hot":true|false}}
Only change status if headlines clearly confirm it. Return has_update:false if nothing new."""

    time.sleep(4)  # stay under 15 req/min free tier limit
    time.sleep(7)  # avoid rate limit (free tier: 10 req/min for 2.5-flash)
    response = ask_gemini(prompt)
    if not response:
        return claim
    try:
        clean = re.sub(r"```[a-z]*\n?", "", response).strip()
        result = json.loads(clean)
    except:
        return claim

    if result.get("has_update") and result.get("update_text"):
        entry = {"date": TODAY, "text": result["update_text"], "hot": result.get("update_hot", False)}
        existing = claim.get("updates", [])
        if not existing or existing[-1]["text"] != entry["text"]:
            claim.setdefault("updates", []).append(entry)
            print(f"  [{cid}] Update: {entry['text'][:50]}...")

    new_status = result.get("status", status)
    if new_status != status:
        print(f"  [{cid}] Status: {status} -> {new_status}")
        claim["status"] = new_status

    return claim

# ── BREAKING NEWS ─────────────────────────────────────────────────────────────
BREAKING_KEYWORDS = [
    "Iran war US military 2026", "USS Boxer Marines Middle East",
    "Strait of Hormuz 2026", "Iran missile strike 2026",
    "Kharg Island US", "Iran war ceasefire 2026",
]

def is_recent(date_str, days=3):
    """Check if a date string is within the last N days."""
    try:
        from datetime import datetime, timedelta
        # Try parsing common formats like "MAR 21, 2026"
        dt = datetime.strptime(date_str.strip(), "%b %d, %Y")
        return dt >= datetime.utcnow() - timedelta(days=days)
    except:
        return True  # Keep if can't parse

def fetch_breaking_news(existing):
    # Remove items older than 3 days
    existing = [e for e in existing if is_recent(e.get("date", ""))]

    all_headlines = []
    for kw in BREAKING_KEYWORDS:
        all_headlines.extend(fetch_headlines([kw]))
    if not all_headlines or not GEMINI_API_KEY:
        return existing

    existing_texts = " | ".join(e["text"][:80] for e in existing[-5:])
    prompt = f"""Updating a breaking news ticker for an Iran war tracker.
HEADLINES:
{chr(10).join(f"- {h}" for h in all_headlines[:20])}
EXISTING (don't repeat):
{existing_texts}

Pick up to 3 new significant developments. Reply ONLY in JSON (no markdown):
[{{"date":"{TODAY}","text":"under 180 chars","source":"source name","hot":true|false}}]
Return [] if nothing new."""

    time.sleep(4)
    response = ask_gemini(prompt)
    if not response:
        return existing
    try:
        clean = re.sub(r"```[a-z]*\n?", "", response).strip()
        new_items = json.loads(clean)
        if isinstance(new_items, list) and new_items:
            return (new_items + existing)[:15]
    except:
        pass
    return existing

# ── COUNT ─────────────────────────────────────────────────────────────────────
def count_statuses(data):
    counts = {"yes": 0, "no": 0, "partial": 0, "watch": 0}
    for section in data["sections"]:
        for claim in section["claims"]:
            s = claim.get("status", "no")
            counts[s] = counts.get(s, 0) + 1
    return counts

# ── RENDER ────────────────────────────────────────────────────────────────────
def render_verdict(status):
    labels = {"yes":"YES","no":"NO","partial":"PARTIAL","watch":"WATCH"}
    return f'<div class="verdict {status}">{labels.get(status,status.upper())}</div>'

def render_updates(updates, status="watch"):
    if not updates:
        return ""
    html = ""
    for u in updates[-3:]:
        if u.get("hot"):
            cls = "news news-hot"
        else:
            cls = f"news news-{status}"
        html += f'<div class="{cls}"><span class="nd">{u["date"]}</span>{u["text"]}</div>\n'
    return html

def render_claim(claim):
    status = claim.get("status","no")
    sc = " no" if status == "no" else ""
    return f'''
    <div class="claim{sc}">
      {render_verdict(status)}
      <div class="claim-body">
        <div class="claim-text">{claim["text"]}</div>
        <div class="claim-src"><span class="src-tag">SRC</span>{claim["source"]}</div>
        {render_updates(claim.get("updates",[]), status)}
      </div>
    </div>'''

def render_breaking(items):
    if not items:
        return ""
    rows = ""
    for item in items:
        hot = " bi-hot" if item.get("hot") else ""
        rows += f'<div class="breaking-item{hot}"><span class="bi-date">{item["date"]}</span>{item["text"]}<span class="bi-src"> &middot; {item.get("source","")}</span></div>\n'
    return f'''<div class="breaking-box">
  <div class="breaking-label">Breaking &mdash; {TODAY}</div>
  {rows}
</div>'''

# ── BUILD HTML ────────────────────────────────────────────────────────────────
def build_html(data):
    counts = count_statuses(data)
    total  = sum(counts.values())
    meta   = data["meta"]
    pct    = round((counts["yes"] + counts["partial"]*0.5) / total * 100)
    pct_bar = round((counts["yes"] + counts["partial"]*0.5 + counts["watch"]*0.25) / total * 100)

    sections_html = ""
    for section in data["sections"]:
        claims_html = "".join(render_claim(c) for c in section["claims"])
        sections_html += f'''
  <div class="section">
    <div class="sec-header"><div class="sec-line"></div><div class="sec-title">{section["title"]}</div><div class="sec-line"></div></div>
    {claims_html}
  </div>'''

    breaking_html = render_breaking(data.get("breaking_news", []))

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>4chan WW3 Prediction \u2014 Fact Check</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@300;600;900&family=Barlow:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0a0c0f;--surface:#111418;--surface2:#181c22;--border:#252b35;--yes:#1a4a2e;--yes-t:#4ade80;--yes-b:#166534;--no:#3f1a1a;--no-t:#f87171;--no-b:#7f1d1d;--partial:#3d2f0d;--partial-t:#fbbf24;--partial-b:#78350f;--watch:#1a2a4a;--watch-t:#60a5fa;--watch-b:#1e3a5f;--tp:#e8ecf0;--ts:#8b95a1;--tm:#4a5568;--red:#ff3333;--mono:"Share Tech Mono",monospace;--display:"Barlow Condensed",sans-serif;--body:"Barlow",sans-serif;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--tp);font-family:var(--body);font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1100px;margin:0 auto;padding:0 32px 80px}}
.header{{padding:40px 0 28px;border-bottom:1px solid var(--border);margin-bottom:32px}}
.stamp-row{{display:flex;align-items:center;gap:12px;margin-bottom:16px}}
.stamp{{font-family:var(--mono);font-size:11px;letter-spacing:.14em;color:var(--red);border:1px solid var(--red);padding:3px 10px}}
.header-meta{{font-family:var(--mono);font-size:11px;color:var(--tm);letter-spacing:.06em}}
h1{{font-family:var(--display);font-weight:900;font-size:72px;line-height:.92;text-transform:uppercase;letter-spacing:-.01em;margin-bottom:12px}}
h1 span{{color:var(--red)}}
.header-sub{{font-family:var(--display);font-weight:300;font-size:17px;letter-spacing:.14em;text-transform:uppercase;color:var(--ts)}}
.top-grid{{display:flex;flex-direction:column;gap:24px;margin-bottom:32px}}
.post-img-wrap{{position:relative;max-width:700px}}
.section-label{{font-family:var(--mono);font-size:10px;letter-spacing:.16em;color:var(--tm);text-transform:uppercase;margin-bottom:10px}}
.post-img-wrap img{{width:100%;border-radius:4px;border:1px solid var(--border);opacity:.88;display:block}}
.img-caption{{font-family:var(--mono);font-size:10px;color:var(--tm);margin-top:7px}}
.right-col{{display:flex;flex-direction:column;gap:16px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.stat{{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 12px 12px;position:relative;overflow:hidden}}
.stat::before{{content:"";position:absolute;top:0;left:0;right:0;height:3px}}
.stat.yes::before{{background:var(--yes-t)}}.stat.partial::before{{background:var(--partial-t)}}.stat.watch::before{{background:var(--watch-t)}}.stat.no::before{{background:var(--no-t)}}
.stat-n{{font-family:var(--display);font-weight:700;font-size:48px;line-height:1;margin-bottom:4px}}
.stat.yes .stat-n{{color:var(--yes-t)}}.stat.partial .stat-n{{color:var(--partial-t)}}.stat.watch .stat-n{{color:var(--watch-t)}}.stat.no .stat-n{{color:var(--no-t)}}
.stat-l{{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--tm);text-transform:uppercase}}
.acc-labels{{display:flex;justify-content:space-between;margin-bottom:7px}}
.acc-labels span{{font-family:var(--mono);font-size:11px;color:var(--tm);letter-spacing:.08em;text-transform:uppercase}}
.bar-track{{height:4px;background:var(--surface2);border-radius:2px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--yes-t) 0%,var(--partial-t) 55%,var(--watch-t) 78%,var(--no-t) 100%);width:0;transition:width 1.2s cubic-bezier(.4,0,.2,1)}}
.legend{{display:flex;flex-wrap:wrap;gap:10px 20px}}
.legend-item{{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;color:var(--tm)}}
.breaking-box{{background:#1a0a0a;border:1px solid #7f1d1d;border-left:3px solid var(--red);border-radius:4px;padding:16px 18px;margin-bottom:32px}}
.breaking-label{{font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--red);text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:8px}}
.breaking-label::before{{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--red);animation:blink 1s ease-in-out infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
.breaking-item{{font-family:var(--mono);font-size:12px;color:var(--tp);line-height:1.7;padding:5px 0;border-bottom:1px solid #3f1a1a}}
.breaking-item:last-child{{border-bottom:none;padding-bottom:0}}
.bi-date{{color:var(--red);margin-right:6px;font-size:11px}}
.bi-src{{color:var(--tm);font-size:10px;margin-left:6px}}
.bi-hot{{color:#fca5a5}}
.section{{margin-bottom:6px}}
.sec-header{{display:flex;align-items:center;gap:12px;padding:20px 0 10px}}
.sec-line{{flex:1;height:1px;background:var(--border)}}
.sec-title{{font-family:var(--mono);font-size:11px;letter-spacing:.16em;color:var(--tm);text-transform:uppercase;white-space:nowrap}}
.claim{{display:flex;align-items:flex-start;gap:14px;padding:13px 14px;border:1px solid transparent;border-radius:4px;margin-bottom:4px;transition:background .15s,border-color .15s}}
.claim:hover{{background:var(--surface2);border-color:var(--border)}}
.verdict{{flex-shrink:0;font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.06em;padding:4px 12px;border-radius:3px;margin-top:2px;min-width:78px;text-align:center;text-transform:uppercase}}
.verdict.yes{{background:var(--yes);color:var(--yes-t);border:1px solid var(--yes-b)}}
.verdict.no{{background:var(--no);color:var(--no-t);border:1px solid var(--no-b)}}
.verdict.partial{{background:var(--partial);color:var(--partial-t);border:1px solid var(--partial-b)}}
.verdict.watch{{background:var(--watch);color:var(--watch-t);border:1px solid var(--watch-b);animation:pulse 2s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.55}}}}
.claim-body{{flex:1;min-width:0}}
.claim-text{{font-size:15px;color:var(--tp);line-height:1.45;margin-bottom:5px}}
.claim.no .claim-text{{text-decoration:line-through;text-decoration-color:rgba(248,113,113,.4);text-decoration-thickness:1.5px;opacity:.6}}
.claim-src{{font-family:var(--mono);font-size:11px;color:var(--tm);line-height:1.55}}
.src-tag{{color:var(--ts);font-size:10px;margin-right:4px;letter-spacing:.05em}}
.news{{font-family:var(--mono);font-size:11px;margin-top:7px;padding:7px 11px;border-radius:0 4px 4px 0;line-height:1.6;border-left:2px solid transparent}}
.nd{{font-size:10px;margin-right:4px}}
.news a{{text-decoration:underline;text-underline-offset:2px;opacity:.8}}
.news.news-yes{{background:rgba(26,74,46,.35);border-left-color:var(--yes-b);color:var(--yes-t)}}
.news.news-yes .nd{{color:#166534}}
.news.news-no{{background:rgba(63,26,26,.35);border-left-color:var(--no-b);color:var(--no-t)}}
.news.news-no .nd{{color:#991b1b}}
.news.news-partial{{background:rgba(61,47,13,.45);border-left-color:var(--partial-b);color:var(--partial-t)}}
.news.news-partial .nd{{color:#92620a}}
.news.news-watch{{background:rgba(30,58,95,.4);border-left-color:var(--watch-b);color:var(--watch-t)}}
.news.news-watch .nd{{color:#4a6b8a}}
.news.news-hot{{background:rgba(63,26,26,.7)!important;border-left-color:#ef4444!important;color:#fca5a5!important}}
.news.news-hot .nd{{color:#ef4444!important}}
.footer{{margin-top:40px;padding:22px 24px;border:1px solid var(--border);background:var(--surface);border-radius:4px}}
.footer p{{font-size:14px;color:var(--ts);line-height:1.75;font-weight:300;margin-bottom:12px}}
.footer strong{{color:var(--tp);font-weight:500}}
.footer-meta{{margin-top:18px;padding-top:16px;border-top:1px solid var(--border);display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}}
.footer-meta span{{font-family:var(--mono);font-size:10px;color:var(--tm);letter-spacing:.07em}}
.nav-toggle-btn{{position:fixed;bottom:30px;right:30px;width:70px;height:70px;border-radius:50%;background:linear-gradient(135deg,rgba(40,40,40,.95),rgba(20,20,20,.95));border:3px solid rgba(255,255,255,.3);box-shadow:0 5px 25px rgba(0,0,0,.5);cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:10500;transition:all .3s ease}}
.nav-toggle-btn:hover{{transform:scale(1.1);box-shadow:0 8px 35px rgba(0,0,0,.7);border-color:rgba(255,255,255,.6)}}
.nav-toggle-btn .icon{{font-size:28px;margin-bottom:3px}}
.nav-toggle-btn .label{{font-size:10px;color:#fff;text-transform:uppercase;letter-spacing:1px;text-shadow:1px 1px 2px rgba(0,0,0,.9)}}
.wheel-overlay{{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.85);backdrop-filter:blur(10px);display:none;justify-content:center;align-items:center;z-index:10600;opacity:0;transition:opacity .3s ease}}
.wheel-overlay.active{{display:flex;opacity:1}}
.close-btn{{position:absolute;top:30px;right:30px;width:50px;height:50px;border-radius:50%;background:rgba(220,38,38,.9);border:2px solid rgba(255,255,255,.3);color:white;font-size:24px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .3s ease}}
.close-btn:hover{{background:rgba(239,68,68,1);transform:scale(1.1)}}
.wheel-container{{position:relative;width:600px;height:600px;display:flex;justify-content:center;align-items:center;transform:scale(.8);transition:transform .3s ease}}
.wheel-overlay.active .wheel-container{{transform:scale(1)}}
.wheel{{position:relative;width:100%;height:100%;border-radius:50%;background:rgba(50,50,50,.95);box-shadow:0 0 50px rgba(0,0,0,.8),inset 0 0 30px rgba(0,0,0,.5);transform:rotate(-90deg)}}
.wheel-segment{{position:absolute;width:50%;height:50%;transform-origin:100% 100%;cursor:pointer;transition:all .3s ease;clip-path:polygon(0 0,100% 0,100% 100%)}}
.wheel-segment::before{{content:\'\';position:absolute;inset:0;background:rgba(80,80,80,.7);border:2px solid rgba(150,150,150,.3);clip-path:polygon(0 0,100% 0,100% 100%);transition:all .3s ease}}
.wheel-segment:hover::before{{background:rgba(120,120,120,.9);border-color:rgba(255,255,255,.6)}}
.wheel-segment.active::before{{background:rgba(140,200,255,.8);border-color:rgba(255,255,255,.9)}}
.segment-content{{position:absolute;top:20%;left:60%;text-align:center;pointer-events:none;width:100px;display:flex;flex-direction:column;align-items:center;justify-content:center}}
.segment-icon{{font-size:32px;margin-bottom:5px;filter:drop-shadow(2px 2px 2px rgba(0,0,0,.8))}}
.segment-label{{font-size:12px;color:#fff;text-transform:uppercase;font-weight:bold;text-shadow:2px 2px 4px rgba(0,0,0,.9);letter-spacing:.5px}}
.wheel-center{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:200px;height:200px;border-radius:50%;background:linear-gradient(135deg,rgba(40,40,40,.95),rgba(20,20,20,.95));box-shadow:0 0 20px rgba(0,0,0,.8),inset 0 0 15px rgba(0,0,0,.5);display:flex;flex-direction:column;justify-content:center;align-items:center;border:3px solid rgba(100,100,100,.5);z-index:10;cursor:pointer}}
.center-text{{color:#fff;font-size:18px;text-transform:uppercase;letter-spacing:2px;text-shadow:2px 2px 4px rgba(0,0,0,.9);margin-bottom:8px}}
.page-title{{color:#5bc0de;font-size:14px;text-transform:uppercase;letter-spacing:1px;text-shadow:2px 2px 4px rgba(0,0,0,.9)}}
.wheel-instruction{{position:absolute;bottom:40px;left:50%;transform:translateX(-50%);color:rgba(255,255,255,.9);font-size:16px;text-shadow:2px 2px 4px rgba(0,0,0,.9);text-align:center;white-space:nowrap}}
.segment-0{{transform:rotate(0deg)}}.segment-1{{transform:rotate(45deg)}}.segment-2{{transform:rotate(90deg)}}.segment-3{{transform:rotate(135deg)}}.segment-4{{transform:rotate(180deg)}}.segment-5{{transform:rotate(225deg)}}.segment-6{{transform:rotate(270deg)}}.segment-7{{transform:rotate(315deg)}}
.segment-0 .segment-content{{transform:rotate(90deg);top:25%;left:60%}}.segment-1 .segment-content{{transform:rotate(45deg);top:20%;left:55%}}.segment-2 .segment-content{{transform:rotate(360deg);top:25%;left:57%}}.segment-3 .segment-content{{transform:rotate(315deg);top:25%;left:55%}}.segment-4 .segment-content{{transform:rotate(270deg);top:25%;left:55%}}.segment-5 .segment-content{{transform:rotate(225deg);top:25%;left:52%}}.segment-6 .segment-content{{transform:rotate(180deg);top:25%;left:55%}}.segment-7 .segment-content{{transform:rotate(137deg);top:25%;left:60%}}
@media(max-width:768px){{.wheel-container{{width:75vmin!important;height:75vmin!important;max-width:340px!important;max-height:340px!important}}.wheel-center{{width:110px!important;height:110px!important}}.nav-toggle-btn{{width:60px!important;height:60px!important;bottom:20px!important;right:20px!important}}.close-btn{{width:45px!important;height:45px!important;font-size:22px!important;top:10px!important;right:10px!important}}}}
</style>
</head>
<body>
<div class="wrap">
<div class="header">
  <div class="stamp-row">
    <div class="stamp">UNVERIFIED SOURCE</div>
    <div class="header-meta">4CHAN /POL/ &middot; POST NO.{meta["post_id"]} &middot; ORIGIN DATE: {meta["post_date"]} &middot; AUTO-UPDATED HOURLY</div>
  </div>
  <h1>4chan WW3<br><span>prediction</span><br>vs reality</h1>
  <div class="header-sub">Fact-check &middot; Updated {TODAY} &middot; {total} claims evaluated</div>
</div>
<div class="top-grid">
  <div class="post-img-wrap">
    <div class="section-label">Original post</div>
    <img src="{IMAGE_PATH}" alt="Original 4chan WW3 prediction post">
    <div class="img-caption">archive.4plebs.org/pol/thread/507977706 &middot; No.{meta["post_id"]}</div>
  </div>
  <div class="right-col">
    <div class="stats">
      <div class="stat yes"><div class="stat-n">{counts["yes"]}</div><div class="stat-l">Confirmed</div></div>
      <div class="stat partial"><div class="stat-n">{counts["partial"]}</div><div class="stat-l">Partial</div></div>
      <div class="stat watch"><div class="stat-n">{counts["watch"]}</div><div class="stat-l">Watch</div></div>
      <div class="stat no"><div class="stat-n">{counts["no"]}</div><div class="stat-l">No</div></div>
    </div>
    <div class="acc-bar">
      <div class="acc-labels"><span>Accuracy</span><span>~{pct}% confirmed</span></div>
      <div class="bar-track"><div class="bar-fill" id="bar"></div></div>
    </div>
    <div class="legend">
      <div class="legend-item"><span class="verdict yes" style="animation:none;font-size:10px;padding:2px 8px;min-width:0">YES</span> Confirmed happened</div>
      <div class="legend-item"><span class="verdict partial" style="animation:none;font-size:10px;padding:2px 8px;min-width:0">PARTIAL</span> Partly true</div>
      <div class="legend-item"><span class="verdict watch" style="animation:none;font-size:10px;padding:2px 8px;min-width:0">WATCH</span> Developing</div>
      <div class="legend-item"><span class="verdict no" style="animation:none;font-size:10px;padding:2px 8px;min-width:0">NO</span> Has not happened</div>
    </div>
  </div>
</div>
{breaking_html}
{sections_html}
<div class="footer">
  <p>The post correctly predicted the <strong>opening act</strong> &mdash; Israel striking Iran, B-2 bunker-buster strikes on nuclear sites, Hormuz closure, oil price spike, and three carriers deploying.</p>
  <p>Several scenarios remain <strong>actively developing</strong>: amphibious forces converging on the region, Turkey under pressure from Iranian missiles, China exploiting US distraction, North Korea conducting nuclear naval tests while THAAD is repositioned.</p>
  <p>Civil war, nuclear exchange, alien invasion, and 5 billion dead have <strong>not materialized</strong>. This remains a regional air/naval war &mdash; but its edges are widening daily.</p>
  <div class="footer-meta">
    <span>UPDATED: {TODAY}</span>
    <span>POST: {meta["post_date"]} &middot; NO.{meta["post_id"]}</span>
    <span>SOURCES: USNI &middot; AP &middot; WSJ &middot; AEI/ISW &middot; MEI &middot; PBS &middot; AL JAZEERA &middot; REUTERS &middot; WIKIPEDIA</span>
  </div>
</div>
</div>
<div class="nav-toggle-btn" id="toggleBtn"><div class="icon">&#127919;</div><div class="label">Menu</div></div>
<div class="wheel-overlay" id="wheelOverlay">
  <div class="close-btn" id="closeBtn">&#x2715;</div>
  <div class="wheel-container">
    <div class="wheel" id="wheel">
      <div class="wheel-segment segment-0" data-page="Pics.html"><div class="segment-content"><div class="segment-icon">&#128444;&#65039;</div><div class="segment-label">Pictures</div></div></div>
      <div class="wheel-segment segment-1" data-page="clock.html"><div class="segment-content"><div class="segment-icon">&#128336;</div><div class="segment-label">Clock</div></div></div>
      <div class="wheel-segment segment-2" data-page="gematria-calculator.html"><div class="segment-content"><div class="segment-icon">&#128290;</div><div class="segment-label">Calculator</div></div></div>
      <div class="wheel-segment segment-3" data-page="BTC Timeline Cycle.html"><div class="segment-content"><div class="segment-icon">&#128002;</div><div class="segment-label">Cycle</div></div></div>
      <div class="wheel-segment segment-4" data-page="local-posts-viewer.html"><div class="segment-content"><div class="segment-icon">&#128221;</div><div class="segment-label">Posts</div></div></div>
      <div class="wheel-segment segment-5" data-page="ww3-tracker.html"><div class="segment-content"><div class="segment-icon">&#128165;</div><div class="segment-label">WW3</div></div></div>
      <div class="wheel-segment segment-6" data-page=""><div class="segment-content"><div class="segment-icon">&#10067;</div><div class="segment-label">Empty</div></div></div>
      <div class="wheel-segment segment-7" data-page=""><div class="segment-content"><div class="segment-icon">&#10067;</div><div class="segment-label">Empty</div></div></div>
    </div>
    <div class="wheel-center" onclick="window.location.href="index.html"">
      <div class="center-text">&#127968; HOME</div>
      <div class="page-title" id="pageTitle">Select Page</div>
    </div>
  </div>
  <div class="wheel-instruction">Hover to preview &middot; Click to navigate &middot; ESC to close</div>
</div>
<script>
  setTimeout(function(){{document.getElementById("bar").style.width="{pct_bar}%";}},300);
  var tb=document.getElementById("toggleBtn"),cb=document.getElementById("closeBtn"),wo=document.getElementById("wheelOverlay"),segs=document.querySelectorAll(".wheel-segment"),pt=document.getElementById("pageTitle");
  tb.addEventListener("click",function(){{wo.classList.add("active");document.body.style.overflow="hidden";}});
  function cw(){{wo.classList.remove("active");document.body.style.overflow="auto";segs.forEach(function(s){{s.classList.remove("active");}});pt.textContent="Select Page";}}
  cb.addEventListener("click",cw);
  wo.addEventListener("click",function(e){{if(e.target===wo)cw();}});
  document.addEventListener("keydown",function(e){{if(e.key==="Escape")cw();}});
  segs.forEach(function(seg){{
    seg.addEventListener("mouseenter",function(){{segs.forEach(function(s){{s.classList.remove("active");}});this.classList.add("active");pt.textContent=this.dataset.page?this.querySelector(".segment-label").textContent:"Coming Soon";}});
    seg.addEventListener("click",function(){{if(this.dataset.page)window.location.href=this.dataset.page;}});
  }});
  document.getElementById("wheel").addEventListener("mouseleave",function(){{segs.forEach(function(s){{s.classList.remove("active");}});pt.textContent="Select Page";}});
</script>
</body>
</html>'''

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"=== WW3 Tracker Update {TODAY} ===")
    data = load_claims()
    print("\n[1/3] Fetching breaking news...")
    data["breaking_news"] = fetch_breaking_news(data.get("breaking_news", []))
    print("\n[2/3] Checking claims...")
    for section in data["sections"]:
        for i, claim in enumerate(section["claims"]):
            section["claims"][i] = update_claim(claim)
    data["meta"]["last_updated"] = TODAY_ISO
    save_claims(data)
    print("\n[3/3] Saving claims.json \u2713")
    html = build_html(data)
    with open(HTML_OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"      Rebuilt {HTML_OUT_FILE} \u2713")
    counts = count_statuses(data)
    yes=counts['yes']; partial=counts['partial']; watch=counts['watch']; no=counts['no']
    print(f"\n=== Done. YES:{yes} PARTIAL:{partial} WATCH:{watch} NO:{no} ===" )

if __name__ == "__main__":
    main()
