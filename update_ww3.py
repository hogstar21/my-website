#!/usr/bin/env python3
"""
WW3 Tracker - Daily Auto-Update Script
Uses Google Gemini (free tier) + Google News RSS to update claims and rebuild HTML.
Runs via GitHub Actions every morning.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from xml.etree import ElementTree

# ── CONFIG ──────────────────────────────────────────────────────────────────
CLAIMS_FILE   = "claims.json"
HTML_OUT_FILE = "ww3-tracker.html"
IMAGE_PATH    = "images/4chan-prediction.jpg"   # relative path in repo
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_URL    = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
MAX_HEADLINES = 5   # headlines per claim to send to Gemini
TODAY         = datetime.now(timezone.utc).strftime("%b %d, %Y").upper()
TODAY_ISO     = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ── STEP 1: LOAD CLAIMS ──────────────────────────────────────────────────────
def load_claims():
    with open(CLAIMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_claims(data):
    with open(CLAIMS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── STEP 2: FETCH GOOGLE NEWS RSS ────────────────────────────────────────────
def fetch_headlines(keywords: list[str]) -> list[str]:
    """Fetch top headlines from Google News RSS for a list of keywords."""
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
        print(f"  RSS fetch error for '{keywords[0]}': {e}")
    return headlines

# ── STEP 3: ASK GEMINI ───────────────────────────────────────────────────────
def ask_gemini(prompt: str) -> str:
    if not GEMINI_API_KEY:
        print("  No GEMINI_API_KEY — skipping AI analysis")
        return ""
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512}
    }).encode()
    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
        return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"  Gemini error: {e}")
        return ""

# ── STEP 4: UPDATE A SINGLE CLAIM ────────────────────────────────────────────
def update_claim(claim: dict) -> dict:
    cid   = claim["id"]
    text  = claim["text"]
    status = claim["status"]

    # Skip locked statuses for absurd claims
    if status == "no" and any(word in text.lower() for word in
       ["alien", "mj12", "plf", "5 billion", "world government", "dome of the rock",
        "tsar bomb", "civil war", "colorado", "invaded from"]):
        print(f"  [{cid}] Skipping absurd claim — locked NO")
        return claim

    print(f"  [{cid}] Fetching news: {text[:60]}...")
    headlines = fetch_headlines(claim.get("keywords", [text]))

    if not headlines:
        print(f"  [{cid}] No headlines found")
        return claim

    headline_text = "\n".join(f"- {h}" for h in headlines)

    prompt = f"""You are fact-checking a claim from a 2025 4chan post about WW3 predictions.

CLAIM: "{text}"
CURRENT STATUS: {status.upper()} (yes=confirmed happened, no=has not happened, partial=partly true, watch=developing/reported but not confirmed)

TODAY'S NEWS HEADLINES about this topic:
{headline_text}

Based ONLY on these headlines, answer in this exact JSON format (no markdown, just raw JSON):
{{
  "status": "yes|no|partial|watch",
  "has_update": true|false,
  "update_text": "one sentence summary of the new development, or empty string if no update",
  "update_hot": true|false
}}

Rules:
- Only change status if headlines clearly confirm it changed
- If no relevant headlines, return has_update: false
- update_hot: true only if it's genuinely urgent/breaking
- Keep update_text under 200 characters
- Do not invent or assume — only use what the headlines say
"""

    response = ask_gemini(prompt)
    if not response:
        return claim

    # Parse JSON from Gemini response
    try:
        # Strip markdown code fences if present
        clean = re.sub(r"```[a-z]*\n?", "", response).strip()
        result = json.loads(clean)
    except json.JSONDecodeError:
        print(f"  [{cid}] Could not parse Gemini response")
        return claim

    # Apply updates
    if result.get("has_update") and result.get("update_text"):
        update_entry = {
            "date": TODAY,
            "text": result["update_text"],
            "hot": result.get("update_hot", False)
        }
        # Only add if not duplicate of last update
        existing = claim.get("updates", [])
        if not existing or existing[-1]["text"] != update_entry["text"]:
            claim.setdefault("updates", []).append(update_entry)
            print(f"  [{cid}] New update: {update_entry['text'][:60]}...")

    new_status = result.get("status", status)
    if new_status != status:
        print(f"  [{cid}] Status changed: {status} → {new_status}")
        claim["status"] = new_status

    return claim

# ── STEP 5: FETCH BREAKING NEWS ──────────────────────────────────────────────
BREAKING_KEYWORDS = [
    "Iran war US military 2026",
    "USS Boxer Marines Middle East",
    "Strait of Hormuz 2026",
    "Iran missile strike 2026",
    "Kharg Island US",
    "Iran war ceasefire 2026",
]

def fetch_breaking_news(existing: list) -> list:
    """Fetch top breaking headlines and ask Gemini to summarize new ones."""
    all_headlines = []
    for kw in BREAKING_KEYWORDS:
        hl = fetch_headlines([kw])
        all_headlines.extend(hl)

    if not all_headlines or not GEMINI_API_KEY:
        return existing

    existing_texts = " | ".join(e["text"][:80] for e in existing[-5:])
    headline_block = "\n".join(f"- {h}" for h in all_headlines[:20])

    prompt = f"""You are updating a breaking news ticker for an Iran war tracker page.

RECENT HEADLINES:
{headline_block}

EXISTING BREAKING NEWS (don't repeat these):
{existing_texts}

Pick up to 3 genuinely new and significant developments not already covered.
Respond in this exact JSON format (no markdown):
[
  {{"date": "{TODAY}", "text": "brief summary under 180 chars", "source": "source name", "hot": true|false}},
  ...
]

Return empty array [] if no new significant developments.
hot=true only for very urgent breaking news.
"""

    response = ask_gemini(prompt)
    if not response:
        return existing

    try:
        clean = re.sub(r"```[a-z]*\n?", "", response).strip()
        new_items = json.loads(clean)
        if isinstance(new_items, list) and new_items:
            print(f"  Breaking news: {len(new_items)} new item(s)")
            # Prepend new items, keep last 15 total
            combined = new_items + existing
            return combined[:15]
    except (json.JSONDecodeError, KeyError):
        pass

    return existing

# ── STEP 6: COUNT STATUSES ───────────────────────────────────────────────────
def count_statuses(data: dict) -> dict:
    counts = {"yes": 0, "no": 0, "partial": 0, "watch": 0}
    for section in data["sections"]:
        for claim in section["claims"]:
            s = claim.get("status", "no")
            counts[s] = counts.get(s, 0) + 1
    return counts

# ── STEP 7: BUILD HTML ───────────────────────────────────────────────────────
def render_verdict(status: str) -> str:
    labels = {"yes": "YES", "no": "NO", "partial": "PARTIAL", "watch": "WATCH"}
    return f'<div class="verdict {status}">{labels.get(status, status.upper())}</div>'

def render_updates(updates: list) -> str:
    if not updates:
        return ""
    html = ""
    for u in updates[-3:]:  # show last 3 updates
        cls = "news news-hot" if u.get("hot") else "news"
        html += f'<div class="{cls}"><span class="nd">{u["date"]}</span>{u["text"]}</div>\n'
    return html

def render_claim(claim: dict) -> str:
    status = claim.get("status", "no")
    strikethrough_class = " no" if status == "no" else ""
    return f'''
    <div class="claim{strikethrough_class}">
      {render_verdict(status)}
      <div class="claim-body">
        <div class="claim-text">{claim["text"]}</div>
        <div class="claim-src"><span class="src-tag">SRC</span>{claim["source"]}</div>
        {render_updates(claim.get("updates", []))}
      </div>
    </div>'''

def render_breaking_news(items: list) -> str:
    if not items:
        return ""
    rows = ""
    for item in items:
        hot_class = " bi-hot" if item.get("hot") else ""
        rows += f'<div class="breaking-item{hot_class}"><span class="bi-date">{item["date"]}</span>{item["text"]}<span class="bi-src"> · {item.get("source","")}</span></div>\n'
    return f'''
<div class="breaking-box">
  <div class="breaking-label">Breaking News</div>
  {rows}
</div>'''

def build_html(data: dict) -> str:
    counts = count_statuses(data)
    total  = sum(counts.values())
    meta   = data["meta"]

    # Build sections HTML
    sections_html = ""
    for section in data["sections"]:
        claims_html = "".join(render_claim(c) for c in section["claims"])
        sections_html += f'''
  <div class="section">
    <div class="sec-header">
      <div class="sec-line"></div>
      <div class="sec-title">{section["title"]}</div>
      <div class="sec-line"></div>
    </div>
    {claims_html}
  </div>'''

    breaking_html = render_breaking_news(data.get("breaking_news", []))

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>4chan WW3 Prediction — Fact Check</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow+Condensed:wght@300;600;900&family=Barlow:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#0a0c0f; --surface:#111418; --surface2:#181c22; --border:#252b35;
  --yes:#1a4a2e; --yes-t:#4ade80; --yes-b:#166534;
  --no:#3f1a1a;  --no-t:#f87171;  --no-b:#7f1d1d;
  --partial:#3d2f0d; --partial-t:#fbbf24; --partial-b:#78350f;
  --watch:#1a2a4a; --watch-t:#60a5fa; --watch-b:#1e3a5f;
  --tp:#e8ecf0; --ts:#8b95a1; --tm:#4a5568; --red:#ff3333;
  --mono:"Share Tech Mono",monospace;
  --display:"Barlow Condensed",sans-serif;
  --body:"Barlow",sans-serif;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--tp);font-family:var(--body);font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1100px;margin:0 auto;padding:0 32px 80px}}

/* HEADER */
.header{{padding:40px 0 28px;border-bottom:1px solid var(--border);margin-bottom:32px}}
.stamp-row{{display:flex;align-items:center;gap:12px;margin-bottom:16px}}
.stamp{{font-family:var(--mono);font-size:11px;letter-spacing:.14em;color:var(--red);border:1px solid var(--red);padding:3px 10px}}
.header-meta{{font-family:var(--mono);font-size:11px;color:var(--tm);letter-spacing:.06em}}
h1{{font-family:var(--display);font-weight:900;font-size:72px;line-height:.92;text-transform:uppercase;letter-spacing:-.01em;margin-bottom:12px}}
h1 span{{color:var(--red)}}
.header-sub{{font-family:var(--display);font-weight:300;font-size:17px;letter-spacing:.14em;text-transform:uppercase;color:var(--ts)}}

/* TOP GRID */
.top-grid{{display:grid;grid-template-columns:400px 1fr;gap:28px;margin-bottom:32px;align-items:start}}
.post-img-wrap{{position:sticky;top:24px}}
.section-label{{font-family:var(--mono);font-size:10px;letter-spacing:.16em;color:var(--tm);text-transform:uppercase;margin-bottom:10px}}
.post-img-wrap img{{width:100%;border-radius:4px;border:1px solid var(--border);opacity:.88;display:block}}
.img-caption{{font-family:var(--mono);font-size:10px;color:var(--tm);margin-top:7px}}
.right-col{{display:flex;flex-direction:column;gap:16px}}

/* STATS */
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.stat{{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:14px 12px 12px;position:relative;overflow:hidden}}
.stat::before{{content:"";position:absolute;top:0;left:0;right:0;height:3px}}
.stat.yes::before{{background:var(--yes-t)}}.stat.partial::before{{background:var(--partial-t)}}
.stat.watch::before{{background:var(--watch-t)}}.stat.no::before{{background:var(--no-t)}}
.stat-n{{font-family:var(--display);font-weight:700;font-size:48px;line-height:1;margin-bottom:4px}}
.stat.yes .stat-n{{color:var(--yes-t)}}.stat.partial .stat-n{{color:var(--partial-t)}}
.stat.watch .stat-n{{color:var(--watch-t)}}.stat.no .stat-n{{color:var(--no-t)}}
.stat-l{{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--tm);text-transform:uppercase}}

/* BAR */
.acc-bar{{}}
.acc-labels{{display:flex;justify-content:space-between;margin-bottom:7px}}
.acc-labels span{{font-family:var(--mono);font-size:11px;color:var(--tm);letter-spacing:.08em;text-transform:uppercase}}
.bar-track{{height:4px;background:var(--surface2);border-radius:2px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--yes-t) 0%,var(--partial-t) 55%,var(--watch-t) 78%,var(--no-t) 100%);width:0;transition:width 1.2s cubic-bezier(.4,0,.2,1)}}

/* LEGEND */
.legend{{display:flex;flex-wrap:wrap;gap:10px 20px}}
.legend-item{{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11px;color:var(--tm)}}

/* BREAKING */
.breaking-box{{background:#1a0a0a;border:1px solid #7f1d1d;border-left:3px solid var(--red);border-radius:4px;padding:16px 18px;margin-bottom:32px}}
.breaking-label{{font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--red);text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:8px}}
.breaking-label::before{{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--red);animation:blink 1s ease-in-out infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
.breaking-item{{font-family:var(--mono);font-size:12px;color:var(--tp);line-height:1.7;padding:5px 0;border-bottom:1px solid #3f1a1a}}
.breaking-item:last-child{{border-bottom:none;padding-bottom:0}}
.bi-date{{color:var(--red);margin-right:6px;font-size:11px}}
.bi-src{{color:var(--tm);font-size:10px;margin-left:6px}}
.bi-hot{{color:#fca5a5}}

/* SECTION */
.section{{margin-bottom:6px}}
.sec-header{{display:flex;align-items:center;gap:12px;padding:20px 0 10px}}
.sec-line{{flex:1;height:1px;background:var(--border)}}
.sec-title{{font-family:var(--mono);font-size:11px;letter-spacing:.16em;color:var(--tm);text-transform:uppercase;white-space:nowrap}}

/* CLAIM */
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
.news{{font-family:var(--mono);font-size:11px;margin-top:7px;padding:7px 11px;background:rgba(30,58,95,.4);border-left:2px solid var(--watch-b);border-radius:0 4px 4px 0;color:var(--watch-t);line-height:1.6}}
.nd{{color:var(--tm);font-size:10px;margin-right:4px}}
.news a{{color:var(--watch-t);text-decoration:underline;text-underline-offset:2px;opacity:.8}}
.news.news-hot{{background:rgba(63,26,26,.5);border-left-color:var(--no-b);color:#fca5a5}}
.news.news-hot .nd{{color:var(--red)}}

/* FOOTER */
.footer{{margin-top:40px;padding:22px 24px;border:1px solid var(--border);background:var(--surface);border-radius:4px}}
.footer p{{font-size:14px;color:var(--ts);line-height:1.75;font-weight:300;margin-bottom:12px}}
.footer strong{{color:var(--tp);font-weight:500}}
.footer-meta{{margin-top:18px;padding-top:16px;border-top:1px solid var(--border);display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}}
.footer-meta span{{font-family:var(--mono);font-size:10px;color:var(--tm);letter-spacing:.07em}}
</style>
</head>
<body>
<div class="wrap">

<div class="header">
  <div class="stamp-row">
    <div class="stamp">UNVERIFIED SOURCE</div>
    <div class="header-meta">4CHAN /POL/ · POST NO.{meta["post_id"]} · ORIGIN DATE: {meta["post_date"]} · AUTO-UPDATED DAILY</div>
  </div>
  <h1>4chan WW3<br><span>prediction</span><br>vs reality</h1>
  <div class="header-sub">Fact-check · Updated {data["meta"]["last_updated"]} · {total} claims evaluated</div>
</div>

<div class="top-grid">
  <div class="post-img-wrap">
    <div class="section-label">Original post</div>
    <img src="{IMAGE_PATH}" alt="Original 4chan WW3 prediction post">
    <div class="img-caption">archive.4plebs.org/pol/thread/507977706 · No.{meta["post_id"]}</div>
  </div>
  <div class="right-col">
    <div class="stats">
      <div class="stat yes"><div class="stat-n">{counts["yes"]}</div><div class="stat-l">Confirmed</div></div>
      <div class="stat partial"><div class="stat-n">{counts["partial"]}</div><div class="stat-l">Partial</div></div>
      <div class="stat watch"><div class="stat-n">{counts["watch"]}</div><div class="stat-l">Watch</div></div>
      <div class="stat no"><div class="stat-n">{counts["no"]}</div><div class="stat-l">No</div></div>
    </div>
    <div class="acc-bar">
      <div class="acc-labels"><span>Accuracy</span><span>~{round((counts["yes"] + counts["partial"] * 0.5) / total * 100)}% confirmed</span></div>
      <div class="bar-track"><div class="bar-fill" id="bar"></div></div>
    </div>
    <div class="legend">
      <div class="legend-item"><span class="verdict yes" style="animation:none;font-size:10px;padding:2px 8px;min-width:0">YES</span> Confirmed happened</div>
      <div class="legend-item"><span class="verdict partial" style="animation:none;font-size:10px;padding:2px 8px;min-width:0">PARTIAL</span> Partly true</div>
      <div class="legend-item"><span class="verdict watch" style="animation:none;font-size:10px;padding:2px 8px;min-width:0">WATCH</span> Developing / reported</div>
      <div class="legend-item"><span class="verdict no" style="animation:none;font-size:10px;padding:2px 8px;min-width:0">NO</span> Has not happened</div>
    </div>
  </div>
</div>

{breaking_html}

{sections_html}

<div class="footer">
  <p>The post correctly predicted the <strong>opening act</strong> — Israel striking Iran, B-2 bunker-buster strikes on nuclear sites, Hormuz closure, oil price spike, and three carriers deploying.</p>
  <p>Several scenarios remain <strong>actively developing</strong>: amphibious forces converging on the region, Turkey under pressure from Iranian missiles, China exploiting US distraction, North Korea conducting nuclear naval tests while THAAD is repositioned.</p>
  <p>Civil war, nuclear exchange, alien invasion, and 5 billion dead have <strong>not materialized</strong>. This remains a regional air/naval war — but its edges are widening daily.</p>
  <div class="footer-meta">
    <span>AUTO-UPDATED: {TODAY}</span>
    <span>POST: {meta["post_date"]} · NO.{meta["post_id"]}</span>
    <span>SOURCES: USNI · AP · WSJ · AEI/ISW · MEI · PBS · AL JAZEERA · REUTERS · WIKIPEDIA</span>
  </div>
</div>

</div>
<script>
  const pct = {round((counts["yes"] + counts["partial"] * 0.5 + counts["watch"] * 0.25) / total * 100)};
  setTimeout(() => {{ document.getElementById("bar").style.width = pct + "%"; }}, 300);
</script>
</body>
</html>'''

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"=== WW3 Tracker Update — {TODAY} ===")

    data = load_claims()

    # Update breaking news
    print("\n[1/3] Fetching breaking news...")
    data["breaking_news"] = fetch_breaking_news(data.get("breaking_news", []))

    # Update each claim
    print("\n[2/3] Checking claims...")
    for section in data["sections"]:
        for i, claim in enumerate(section["claims"]):
            section["claims"][i] = update_claim(claim)

    # Update timestamp
    data["meta"]["last_updated"] = TODAY_ISO

    # Save updated claims
    save_claims(data)
    print("\n[3/3] Saving claims.json ✓")

    # Rebuild HTML
    html = build_html(data)
    with open(HTML_OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"      Rebuilt {HTML_OUT_FILE} ✓")

    counts = count_statuses(data)
    print(f"\n=== Done. YES:{counts['yes']} PARTIAL:{counts['partial']} WATCH:{counts['watch']} NO:{counts['no']} ===")

if __name__ == "__main__":
    main()
