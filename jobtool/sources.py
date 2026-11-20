"""Job sources that are not company boards: Built In Boston and LinkedIn's public job search.

Neither needs an account. LinkedIn is read the way a logged-out visitor sees it, so your own
LinkedIn account is never touched. Both run through the same title/location filters as the boards."""
import html
import random
import re
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .boards import Filters

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


class Throttled(Exception):
    pass


def get_html(url, timeout=25, backoff=(30, 90)):
    """GET a page. On HTTP 429 wait and retry (30s, then 90s); still throttled -> Throttled."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html",
                                               "Accept-Language": "en-US,en;q=0.9"})
    for wait in (*backoff, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            if wait is None:
                raise Throttled("HTTP 429 after waiting")
            print(f"     ...LinkedIn says slow down, waiting {wait}s")
            time.sleep(wait)


def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ------------------------------------------------------------------ "remote" sanity check
# Job sites (LinkedIn especially) label lots of on-site/hybrid jobs as remote. For jobs we only kept
# because they're remote, read the description and drop them if it says otherwise.
NOT_REMOTE_RE = re.compile(
    r"relocat(e|ion) (to|assistance)|willing(ness)? to relocate|must (live|reside|be (located|based)) (in|within|near)|"
    r"(on-?site|in[- ]office|in[- ]person)\b[^.]{0,60}\b(days?|required|requirement|position|role|5x|\d x)|"
    r"\bhybrid\b|commut(e|ing) (to|distance)|report(ing)? to (our|the) office", re.I)
REALLY_REMOTE_RE = re.compile(
    r"fully remote|100% remote|remote[- ]first|work from anywhere|remote (anywhere )?(in|within) the (us|u\.s\.|united states)|"
    r"(this|the) (is a )?(role|position) is (fully )?remote", re.I)


def remote_conflict(description, flt=None):
    """True if a 'remote' job's description puts you on-site, hybrid or relocating somewhere OUTSIDE
    your target area. On-site/relocate in a place from config `places` (e.g. Boston, Cambridge, MA) is fine."""
    d = description or ""
    if REALLY_REMOTE_RE.search(d):
        return False
    hits = list(NOT_REMOTE_RE.finditer(d))
    if not hits:
        return False
    if flt is None:
        return True
    for m in hits:
        window = d[max(0, m.start() - 150): m.end() + 150]
        if not in_target_area(flt, window):
            return True        # on-site / relocate somewhere you don't want
    return False               # every on-site / relocate mention is in your area


def in_target_area(flt, loc):
    return bool(flt.places.search(loc or "") or flt.state.search(loc or ""))


# ------------------------------------------------------------------ LinkedIn (logged out)
LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LI_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
PAST = {"day": "r86400", "week": "r604800", "month": "r2592000"}


LINKEDIN_OK = {"ok": False}


def _window(sc, s, since):
    """LinkedIn's 'posted within' filter. past: auto (default) searches back to your last run."""
    past = s.get("past", sc.get("past", "auto"))
    if past in PAST:
        return PAST[past]
    if not since:
        return PAST["week"]
    secs = (datetime.now(timezone.utc) - since).total_seconds() + 6 * 3600   # small overlap
    return "r%d" % int(min(max(secs, 86400), 30 * 86400))


def linkedin(cfg, seen, log=print, since=None, on_batch=None):
    sc = cfg.get("sources", {}).get("linkedin", {})
    if not sc.get("enabled"):
        return []
    flt = Filters(cfg)
    found, ids = [], set()
    pause = sc.get("pause_seconds", 8)
    if since is None or sc.get("past", "auto") == "auto":
        w = int(_window(sc, {}, since)[1:]) // 3600
        log(f"     LinkedIn: searching jobs posted in the last {w // 24} day(s) {w % 24}h")
    throttled = False
    for s in sc.get("searches", []):
        if throttled:
            log(f"SKIP LinkedIn '{s['keywords']}': throttled earlier this run")
            continue
        params = {"keywords": s["keywords"], "location": s.get("location", "Boston, Massachusetts, United States"),
                  "f_TPR": _window(sc, s, since), "sortBy": "DD"}
        if s.get("distance"):
            params["distance"] = s["distance"]
        if sc.get("experience"):
            params["f_E"] = ",".join(str(e) for e in sc["experience"])
        if s.get("remote"):
            params["f_WT"] = "2"
        total = kept = 0
        mark = len(found)
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
                        loc = (loc + " (remote?)").strip()   # LinkedIn's remote filter; verified below
                    if not flt.company_ok(company) or not flt.title_ok(title) or \
                            not flt.location_ok(loc, bool(s.get("remote"))):
                        continue
                    found.append({"key": f"li:{jid}", "alt_keys": [], "company": company, "title": title,
                                  "location": loc, "url": f"https://www.linkedin.com/jobs/view/{jid}/",
                                  "ats": "linkedin", "posted": posted})
                    kept += 1
                time.sleep(pause + random.random())
            log(f"OK   LinkedIn '{s['keywords']}'{' remote' if s.get('remote') else ''}: {total} listed, {kept} new")
            if on_batch and not sc.get("resolve_apply_links", False):
                for j in found[mark:]:
                    j["ats"] = "linkedin_check"
                on_batch(found[mark:])      # saved now, not at the end of the run
                del found[mark:]
            time.sleep(pause * 2 + random.random() * pause)   # extra gap between searches
        except Throttled:
            throttled = True
            if on_batch and not sc.get("resolve_apply_links", False) and found[mark:]:
                for j in found[mark:]:
                    j["ats"] = "linkedin_check"
                on_batch(found[mark:])
                del found[mark:]
            log(f"FAIL LinkedIn '{s['keywords']}': still throttled after waiting. Skipping the rest this run; "
                "the jobs found so far are kept. Try again in an hour.")
        except Exception as e:
            log(f"FAIL LinkedIn '{s['keywords']}': {type(e).__name__}: {str(e)[:90]}"
                + (" (LinkedIn is throttling, try again later)" if "429" in str(e) else ""))
    # find where each job is really applied to (company site) so it can be prefilled.
    # This is one extra request per job, so it stops at the first sign of throttling.
    LINKEDIN_OK["ok"] = not throttled
    if not sc.get("resolve_apply_links", False):
        # No per-job lookups (halves LinkedIn requests). Your signed-in `apply --easy` session checks each
        # job's real button: Easy Apply stays there, company-site jobs move to the forms queue.
        for j in found:
            j["ats"] = "linkedin_check"
        return found
    dropped = 0
    for n, j in enumerate(found):
        if throttled:
            j["ats"] = "linkedin"      # still applyable: apply opens the LinkedIn page and clicks Apply
            continue
        try:
            page = get_html(LI_POSTING.format(j["key"][3:]))
            if not in_target_area(flt, j["location"]):
                desc = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
                if remote_conflict(text(desc.group(1) if desc else page), flt):
                    j["_drop"] = True
                    dropped += 1
                    continue
                j["location"] = j["location"].replace("(remote?)", "(Remote)")
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
            time.sleep(pause / 2 + random.random() * 2)
        except Throttled:
            throttled = True
            j["ats"] = "linkedin"
            log(f"     LinkedIn throttled while checking apply links ({n} of {len(found)} done); "
                "the rest will be resolved when you apply")
        except Exception:
            pass
    if dropped:
        log(f"     dropped {dropped} LinkedIn 'remote' job(s) whose description says on-site, hybrid or relocate")
    return [j for j in found if not j.get("_drop")]


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
                        if not flt.title_ok(r["title"]) or not flt.company_ok(r["company"]):
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
    for key, ats in (("greenhouse", "greenhouse"), ("lever.co", "lever"), ("ashbyhq", "ashby"), ("workable", "workable"), ("recruitee", "recruitee"), ("rippling", "rippling"),
                     ("myworkdayjobs", "workday"), ("smartrecruiters", "smartrecruiters"),
                     ("linkedin.com", "linkedin"), ("builtin", "builtin"), ("wellfound", "wellfound")):
        if key in host:
            return ats
    if "gh_jid=" in (url or ""):
        return "greenhouse"
    return "other"


# ------------------------------------------------------------------ shared browser helpers
def _launch(pw, cfg, base_dir, persistent=False, headless=True):
    kw = {"headless": headless}
    if cfg.get("apply", {}).get("browser", "chrome") == "chrome":
        kw["channel"] = "chrome"
    if persistent:   # your signed-in tool browser (same profile apply uses)
        from pathlib import Path
        ctx = pw.chromium.launch_persistent_context(str(Path(base_dir) / "data" / "browser-profile"),
                                                    viewport={"width": 1280, "height": 900}, **kw)
        return ctx, ctx
    browser = pw.chromium.launch(**kw)
    return browser, browser.new_context(user_agent=BROWSER_UA, viewport={"width": 1280, "height": 900})


def _days_ago(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _keep(flt, j, days, seen, log_drop=None):
    if j["key"] in seen or not j["title"]:
        return False
    if not flt.company_ok(j["company"]) or not flt.title_ok(j["title"]):
        return False
    if not flt.location_ok(j["location"], j.get("remote", False)):
        return False
    age = _days_ago(j.get("posted"))
    if age is not None and age > days:
        return False
    if j.get("description") and not in_target_area(flt, j["location"]) and remote_conflict(j["description"], flt):
        return False
    return True


# ------------------------------------------------------------------ hiring.cafe
# hiring.cafe collects jobs straight from company career pages. The tool opens each search URL from the
# config in a hidden browser, scrolls, and records the job data the page itself loads.
def _dig(d, *paths):
    for p in paths:
        cur = d
        for k in p.split("."):
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur not in (None, ""):
            return cur
    return None


def _job_rows(d, depth=0):
    """Find the list of job records in a JSON response, wherever it sits."""
    if depth > 4:
        return []
    if isinstance(d, list):
        dicts = [x for x in d if isinstance(x, dict)]
        if dicts and any(("job_information" in x or "apply_url" in x or "v5_processed_job_data" in x
                          or ("title" in x and ("company" in x or "url" in x))) for x in dicts[:5]):
            return dicts
        for x in dicts[:3]:
            r = _job_rows(x, depth + 1)
            if r:
                return r
        return []
    if isinstance(d, dict):
        for k in ("results", "hits", "jobs", "data", "items"):
            if k in d:
                r = _job_rows(d[k], depth + 1)
                if r:
                    return r
        for v in d.values():
            if isinstance(v, (list, dict)):
                r = _job_rows(v, depth + 1)
                if r:
                    return r
    return []


def _hc_job(r):
    v5 = r.get("v5_processed_job_data") or r.get("processed_job_data") or {}
    title = _dig(r, "job_information.title", "title") or v5.get("core_job_title") or ""
    company = v5.get("company_name") or _dig(r, "enriched_company_data.name", "company_name", "company") or ""
    loc = v5.get("formatted_workplace_location") or _dig(r, "location", "job_information.location") or ""
    wp = (v5.get("workplace_type") or "").lower()
    if wp and wp not in loc.lower():
        loc = f"{loc} ({v5.get('workplace_type')})".strip()
    url = r.get("apply_url") or _dig(r, "job_information.apply_url", "url") or ""
    jid = r.get("id") or r.get("objectID") or url
    desc = _dig(r, "job_information.description", "description") or ""
    comp = ""
    if v5.get("yearly_min_compensation"):
        comp = f"${int(v5['yearly_min_compensation']):,}" + (f"-${int(v5['yearly_max_compensation']):,}"
                                                             if v5.get("yearly_max_compensation") else "")
    from .boards import strip_html
    return {"key": f"hc:{jid}", "alt_keys": [], "company": company, "title": title, "location": loc,
            "remote": "remote" in wp or "remote" in loc.lower(), "url": url, "ats": ats_from_url(url),
            "posted": v5.get("estimated_publish_date") or r.get("created_at"),
            "description": strip_html(desc)[:8000], "comp": comp}


def hiringcafe(cfg, seen, days, base_dir, log=print):
    sc = cfg.get("sources", {}).get("hiringcafe", {})
    if not sc.get("enabled"):
        return []
    from playwright.sync_api import sync_playwright
    from .boards import Filters, info_notes
    flt = Filters(cfg)
    found, ids = [], set()
    with sync_playwright() as pw:
        owner, ctx = _launch(pw, cfg, base_dir, persistent=True, headless=sc.get("headless", False))
        for url in sc.get("searches", []):
            batch, seen_json = [], []

            def grab(resp):
                if "json" not in (resp.headers.get("content-type") or ""):
                    return
                try:
                    d = resp.json()
                except Exception:
                    return
                seen_json.append(resp.url.split("?")[0])
                rows = _job_rows(d)
                batch.extend(rows)

            page = ctx.new_page()
            page.on("response", grab)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                for _ in range(sc.get("scrolls", 6)):     # infinite scroll loads more results
                    page.mouse.wheel(0, 6000)
                    page.wait_for_timeout(1500)
                if not batch:   # some sites ship the first results inside the page itself
                    try:
                        embedded = page.evaluate("() => { const s = document.getElementById('__NEXT_DATA__');"
                                                 " return s ? JSON.parse(s.textContent) : null; }")
                        batch.extend(_job_rows(embedded or {}))
                    except Exception:
                        pass
                kept = 0
                for r in batch:
                    j = _hc_job(r)
                    if j["key"] in ids or not j["url"]:
                        continue
                    ids.add(j["key"])
                    if _keep(flt, j, days, seen):
                        j["notes"] = info_notes(j)
                        j.pop("description", None)
                        found.append(j)
                        kept += 1
                name = re.search(r"searchQuery%22%3A%22([^%]+)", url)
                label = urllib.parse.unquote_plus(name.group(1)) if name else url[:60]
                if not batch:
                    log(f"FAIL hiring.cafe '{label}': no jobs found on the page. Page title: {page.title()[:60]!r}")
                    log(f"     JSON it loaded: {', '.join(dict.fromkeys(seen_json)) or 'none'}  (paste this line to Claude)")
                else:
                    log(f"OK   hiring.cafe '{label}': {len(batch)} listed, {kept} new")
            except Exception as e:
                log(f"FAIL hiring.cafe: {type(e).__name__}: {str(e)[:100]}")
            page.close()
        owner.close()
    return found


# ------------------------------------------------------------------ Wellfound (signed in)
WF_JS = r"""() => {
  const out = [], seen = new Set();
  document.querySelectorAll('a[href*="/jobs/"]').forEach(a => {
    const m = a.href.match(/\/jobs\/(\d+)[-\w]*/);
    if (!m || seen.has(m[1])) return;
    const title = (a.innerText || '').trim().split('\n')[0];
    if (!title || title.length > 140) return;
    let card = a;
    for (let i = 0; i < 8 && card.parentElement; i++) {
      card = card.parentElement;
      if (card.querySelector('a[href*="/company/"]')) break;
    }
    const comp = card.querySelector('a[href*="/company/"]');
    const t = card.innerText || '';
    const loc = (t.match(/(Remote[^\n•]*|[A-Z][a-zA-Z .]+, (MA|Massachusetts)[^\n•]*|In office[^\n•]*|Hybrid[^\n•]*)/) || [''])[0];
    const posted = (t.match(/(today|yesterday|\d+ (hours?|days?|weeks?) ago)/i) || [''])[0];
    const pay = (t.match(/\$\s?\d+k\s?[–-]\s?\$\s?\d+k/i) || [''])[0];
    seen.add(m[1]);
    out.push({id: m[1], title, url: a.href.split('?')[0], location: loc, posted, pay,
              company: comp ? (comp.innerText || '').trim().split('\n')[0] : ''});
  });
  return out;
}"""


def wellfound(cfg, seen, days, base_dir, log=print):
    sc = cfg.get("sources", {}).get("wellfound", {})
    if not sc.get("enabled"):
        return []
    from playwright.sync_api import sync_playwright
    from .boards import Filters
    flt = Filters(cfg)
    found = []
    with sync_playwright() as pw:
        owner, ctx = _launch(pw, cfg, base_dir, persistent=True, headless=False)
        page = ctx.new_page()
        try:
            page.goto("https://wellfound.com/jobs", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            if not page.evaluate(WF_JS):
                log("     Wellfound: no job listings visible yet. In the browser window, log in (or finish any"
                    " 'verify you are human' check) and get to the Jobs page.")
                try:
                    if input("     Press Enter here when you can see jobs (or type s + Enter to skip Wellfound): ").strip().lower() == "s":
                        owner.close()
                        log("SKIP Wellfound")
                        return []
                except EOFError:
                    pass
                if not page.evaluate(WF_JS) and "/jobs" not in page.url:
                    page.goto("https://wellfound.com/jobs", wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
            urls = sc.get("pages") or ["https://wellfound.com/jobs"]
            rows, ids = [], set()
            for u in urls:
                if page.url.rstrip("/") != u.rstrip("/"):
                    page.goto(u, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                for _ in range(sc.get("scrolls", 8)):
                    page.mouse.wheel(0, 5000)
                    page.wait_for_timeout(1500 + random.random() * 1000)
                for r in page.evaluate(WF_JS):
                    if r["id"] not in ids:
                        ids.add(r["id"])
                        rows.append(r)
            if not rows and re.search(r"verify|captcha|blocked|access denied", page.content()[:5000], re.I):
                raise RuntimeError("Wellfound showed a bot check; solve it in the window and run find again")
            now = datetime.now(timezone.utc)
            for r in rows:
                age = _bi_age_days(r["posted"].replace("weeks", "days")) if r["posted"] else None
                if r["posted"] and "week" in r["posted"]:
                    age = int(re.search(r"\d+", r["posted"]).group()) * 7
                j = {"key": f"wf:{r['id']}", "alt_keys": [], "company": r["company"], "title": r["title"],
                     "location": r["location"] or "", "remote": "remote" in (r["location"] or "").lower(),
                     "url": r["url"], "ats": "wellfound", "notes": f"pay {r['pay']}" if r["pay"] else "",
                     "posted": (now - timedelta(days=age)).date().isoformat() if age is not None else None}
                if _keep(flt, j, days, seen):
                    found.append(j)
            log(f"OK   Wellfound: {len(rows)} listed, {len(found)} new")
        except Exception as e:
            log(f"FAIL Wellfound: {type(e).__name__}: {str(e)[:120]}")
        owner.close()
    return found
