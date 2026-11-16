"""Job sources that are not company boards: Built In Boston and LinkedIn's public job search.

Neither needs an account. LinkedIn is read the way a logged-out visitor sees it, so your own
LinkedIn account is never touched. Both run through the same title/location filters as the boards."""
import html
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .boards import Filters

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def get_html(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html",
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ------------------------------------------------------------------ LinkedIn (logged out)
LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LI_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
PAST = {"day": "r86400", "week": "r604800", "month": "r2592000"}


def linkedin(cfg, seen, log=print):
    sc = cfg.get("sources", {}).get("linkedin", {})
    if not sc.get("enabled"):
        return []
    flt = Filters(cfg)
    found, ids = [], set()
    pause = sc.get("pause_seconds", 2)
    for s in sc.get("searches", []):
        params = {"keywords": s["keywords"], "location": s.get("location", "Boston, Massachusetts, United States"),
                  "f_TPR": PAST.get(s.get("past", sc.get("past", "day")), "r86400"), "sortBy": "DD"}
        if s.get("distance"):
            params["distance"] = s["distance"]
        if sc.get("experience"):
            params["f_E"] = ",".join(str(e) for e in sc["experience"])
        if s.get("remote"):
            params["f_WT"] = "2"
        total = kept = 0
        try:
            for start in range(0, sc.get("max_results_per_search", 100), 10):
                page = get_html(LI_SEARCH + "?" + urllib.parse.urlencode({**params, "start": start}))
                cards = re.findall(r"<li>(.*?)</li>", page, re.S) or page.split('<div class="base-card')[1:]
                if not cards:
                    break
                for c in cards:
                    m = re.search(r"jobPosting:(\d+)", c) or re.search(r"/jobs/view/[^\"?]*?-?(\d{8,})", c)
                    if not m:
                        continue
                    jid = m.group(1)
                    total += 1
                    if jid in ids or f"li:{jid}" in seen:
                        continue
                    ids.add(jid)
                    title = text((re.search(r'base-search-card__title[^>]*>(.*?)</h3>', c, re.S) or [None, ""])[1])
                    company = text((re.search(r'base-search-card__subtitle[^>]*>(.*?)</h4>', c, re.S) or [None, ""])[1])
                    loc = text((re.search(r'job-search-card__location[^>]*>(.*?)</span>', c, re.S) or [None, ""])[1])
                    posted = (re.search(r'datetime="([\d-]+)"', c) or [None, None])[1]
                    if s.get("remote") and "remote" not in loc.lower():
                        loc = (loc + " (Remote)").strip()
                    if not flt.title_ok(title) or not flt.location_ok(loc, bool(s.get("remote"))):
                        continue
                    found.append({"key": f"li:{jid}", "alt_keys": [], "company": company, "title": title,
                                  "location": loc, "url": f"https://www.linkedin.com/jobs/view/{jid}/",
                                  "ats": "linkedin", "posted": posted})
                    kept += 1
                time.sleep(pause + random.random())
            log(f"OK   LinkedIn '{s['keywords']}'{' remote' if s.get('remote') else ''}: {total} listed, {kept} new")
        except Exception as e:
            log(f"FAIL LinkedIn '{s['keywords']}': {type(e).__name__}: {str(e)[:90]}"
                + (" (LinkedIn is throttling, try again later)" if "429" in str(e) else ""))
    # find where each job is really applied to (company site) so it can be prefilled
    for j in found:
        try:
            page = get_html(LI_POSTING.format(j["key"][3:]))
            m = re.search(r'id="applyUrl"[^>]*>\s*<!--\s*"?([^"<]+)"?\s*-->', page)
            if m:
                u = html.unescape(m.group(1))
                q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
                real = q.get("url", [u])[0]
                if "linkedin.com" not in real:
                    j["url"] = real
                    j["ats"] = ats_from_url(real)
            else:
                j["ats"] = "linkedin_easy"   # Easy Apply only: you apply on LinkedIn yourself
            time.sleep(pause / 2 + random.random())
        except Exception:
            pass
    return found


# ------------------------------------------------------------------ Built In Boston (browser)
BI_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/job/"]').forEach(a => {
    const m = a.href.match(/\/job\/[^/?#]+\/(\d+)/);
    if (!m || seen.has(m[1])) return;
    const title = (a.innerText || '').trim();
    if (!title || title.length > 140) return;
    let card = a;
    for (let i = 0; i < 8 && card.parentElement; i++) {
      card = card.parentElement;
      if (card.querySelector('a[href*="/company/"]')) break;
    }
    const comp = card.querySelector('a[href*="/company/"]');
    const t = card.innerText || '';
    const loc = (t.match(/(Remote|Hybrid|In-Office)[^\n]*|[A-Z][a-zA-Z .]+, (MA|Massachusetts)[^\n]*/) || [''])[0];
    const posted = (t.match(/(\d+ (minutes?|hours?|days?) ago|Yesterday|Today|Reposted[^\n]*|An? (hour|day) ago)/i) || [''])[0];
    seen.add(m[1]);
    out.push({id: m[1], title, url: a.href.split('?')[0],
              company: comp ? comp.innerText.trim().split('\n')[0] : '', location: loc, posted});
  });
  return out;
}
"""


def _bi_age_days(p):
    p = (p or "").lower()
    if not p or "today" in p or "minute" in p or "hour" in p:
        return 0
    if "yesterday" in p or re.search(r"\ba day", p):
        return 1
    m = re.search(r"(\d+) days?", p)
    return int(m.group(1)) if m else None


def builtin(cfg, seen, days, base_dir, log=print):
    sc = cfg.get("sources", {}).get("builtin", {})
    if not sc.get("enabled"):
        return []
    from playwright.sync_api import sync_playwright
    flt = Filters(cfg)
    found, ids = [], set()
    now = datetime.now(timezone.utc)
    with sync_playwright() as pw:
        kw = {"headless": sc.get("headless", True)}
        if cfg.get("apply", {}).get("browser", "chrome") == "chrome":
            kw["channel"] = "chrome"
        browser = pw.chromium.launch(**kw)
        page = browser.new_page(user_agent=BROWSER_UA)
        for base in sc.get("pages", []):
            total = kept = 0
            try:
                for n in range(1, sc.get("max_pages", 3) + 1):
                    page.goto(base + (f"?page={n}" if n > 1 else ""), wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)
                    rows = page.evaluate(BI_JS)
                    if not rows:
                        if n == 1 and re.search(r"verify you are human|access denied|cloudflare", page.content(), re.I):
                            raise RuntimeError("Built In showed a bot check; set sources.builtin.headless: false")
                        break
                    for r in rows:
                        total += 1
                        if r["id"] in ids or f"bi:{r['id']}" in seen:
                            continue
                        ids.add(r["id"])
                        age = _bi_age_days(r["posted"])
                        if age is not None and age > days:
                            continue
                        if not flt.title_ok(r["title"]):
                            continue
                        loc = r["location"] or "Boston, MA"
                        if not flt.location_ok(loc, "remote" in loc.lower()):
                            continue
                        found.append({"key": f"bi:{r['id']}", "alt_keys": [], "company": r["company"] or "(Built In)",
                                      "title": r["title"], "location": loc, "url": r["url"], "ats": "builtin",
                                      "posted": (now - timedelta(days=age)).date().isoformat() if age is not None else None})
                        kept += 1
                log(f"OK   Built In {base.rstrip('/').split('/')[-1]}: {total} listed, {kept} new")
            except Exception as e:
                log(f"FAIL Built In {base}: {type(e).__name__}: {str(e)[:100]}")
        browser.close()
    return found


def ats_from_url(url):
    host = re.sub(r"^https?://", "", url or "").split("/")[0].lower()
    for key, ats in (("greenhouse", "greenhouse"), ("lever.co", "lever"), ("ashbyhq", "ashby"),
                     ("myworkdayjobs", "workday"), ("smartrecruiters", "smartrecruiters"),
                     ("linkedin.com", "linkedin"), ("builtin", "builtin")):
        if key in host:
            return ats
    if "gh_jid=" in (url or ""):
        return "greenhouse"
    return "other"
