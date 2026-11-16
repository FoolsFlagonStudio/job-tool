"""Fetch open jobs from company job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday)
through their public JSON feeds. No login, no scraping, no AI."""
import json
import re
import time
import urllib.error
import urllib.request
import os
import sys
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (job-tool)", "Accept": "application/json"}


def fetch(url, data=None, timeout=30, retry_429=True):
    body = json.dumps(data).encode() if data is not None else None
    headers = dict(UA)
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and not retry_429:
                raise
            if e.code == 429:              # rate limited: back off and try again
                try:
                    ra = int(e.headers.get("Retry-After") or 0)
                except ValueError:
                    ra = 0
                time.sleep(min(ra or 10 * (attempt + 1), 30))   # never wait more than 30s
                continue
            if e.code in (400, 401, 403, 404) or attempt >= 1:
                raise
            time.sleep(2)
        except Exception as e:
            last = e
            if attempt >= 1:
                raise
            time.sleep(2)
    raise last


def parse_dt(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------- feed readers: each yields dicts with key, title, location, remote, url, posted,
# and (where the feed has it) description, which is used for the remote check, years and salary notes ----------

def strip_html(s):
    import html as _h
    s = _h.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = _h.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def greenhouse(b):
    url = b.get("feed") or f"https://boards-api.greenhouse.io/v1/boards/{b['token']}/jobs"
    url += ("&" if "?" in url else "?") + "content=true"
    for j in fetch(url).get("jobs", []):
        locs = [(j.get("location") or {}).get("name", "")] + [o.get("name", "") for o in (j.get("offices") or [])]
        yield {"key": f"gh:{b['token']}:{j['id']}", "title": j.get("title", ""),
               "location": "; ".join(x for x in dict.fromkeys(locs) if x), "remote": False,
               "url": j.get("absolute_url"), "posted": j.get("first_published") or j.get("updated_at"),
               "ats": "greenhouse", "description": strip_html(j.get("content", ""))}


def lever(b):
    url = b.get("feed") or f"https://api.lever.co/v0/postings/{b['token']}?mode=json"
    for j in fetch(url):
        c = j.get("categories") or {}
        locs = c.get("allLocations") or [c.get("location", "")]
        yield {"key": f"lv:{b['token']}:{j['id']}", "title": j.get("text", ""),
               "location": "; ".join(x for x in locs if x),
               "remote": j.get("workplaceType") == "remote", "url": j.get("hostedUrl"),
               "posted": j.get("createdAt"), "ats": "lever",
               "description": (j.get("descriptionPlain") or "") + "\n" + (j.get("additionalPlain") or ""),
               "comp": (lambda sr: f"${sr['min']:,}-${sr.get('max', 0):,}" if sr.get("min") else "")(j.get("salaryRange") or {})}


def ashby(b):
    url = b.get("feed") or f"https://api.ashbyhq.com/posting-api/job-board/{b['token']}?includeCompensation=true"
    for j in fetch(url).get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
        loc = "; ".join(x for x in locs if x) + (f" ({country})" if country else "")
        yield {"key": f"ab:{b['token']}:{j['id']}", "title": j.get("title", ""), "location": loc,
               "remote": j.get("workplaceType") == "Remote" or bool(j.get("isRemote")), "url": j.get("jobUrl"),
               "posted": j.get("publishedAt"), "ats": "ashby",
               "description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
               "comp": ((j.get("compensation") or {}).get("compensationTierSummary") or "")}


def smartrecruiters(b):
    offset = 0
    base = b.get("feed") or f"https://api.smartrecruiters.com/v1/companies/{b['token']}/postings"
    while True:
        d = fetch(f"{base}?limit=100&offset={offset}")
        for j in d.get("content", []):
            l = j.get("location") or {}
            yield {"key": f"sr:{b['token']}:{j['id']}", "title": j.get("name", ""),
                   "location": ", ".join(x for x in [l.get("city"), l.get("region"), l.get("country")] if x),
                   "remote": bool(l.get("remote")),
                   "url": f"https://jobs.smartrecruiters.com/{b['token']}/{j['id']}",
                   "posted": j.get("releasedDate"), "ats": "smartrecruiters"}
        offset += 100
        if offset >= d.get("totalFound", 0):
            break


WORKDAY_429 = {"streak": 0, "gave_up": False}   # Workday limits by IP across ALL its companies; back off as a group


def workday(b):
    """feed = https://<tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs"""
    if WORKDAY_429["streak"] >= 3:
        WORKDAY_429["gave_up"] = True
        raise RuntimeError("SKIP: Workday is rate limiting, will check again next run")
    feed = b["feed"]
    site_root = feed.split("/wday/cxs/")[0]
    site = feed.rstrip("/").split("/")[-2]
    now = datetime.now(timezone.utc)
    started = time.time()
    for search in ("software engineer", "developer"):
        offset = 0
        while offset < 100 and time.time() - started < 40:   # whole company capped at ~40s
            try:
                d = fetch(feed, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search},
                          timeout=20, retry_429=False)
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise
                if offset + (search == "developer") > 0:
                    return          # rate limited part-way: keep what we already have
                time.sleep(8)       # first page: one polite retry
                try:
                    d = fetch(feed, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": search},
                              timeout=20, retry_429=False)
                except urllib.error.HTTPError as e2:
                    if e2.code == 429:
                        WORKDAY_429["streak"] += 1
                    raise
            WORKDAY_429["streak"] = 0
            posts = d.get("jobPostings", [])
            for j in posts:
                p = (j.get("postedOn") or "").lower()
                days = 0 if "today" in p else 1 if "yesterday" in p else None
                m = re.search(r"(\d+)\+? days", p)
                if m:
                    days = int(m.group(1))
                path = j.get("externalPath", "")
                yield {"key": f"wd:{b['token']}:{path.rstrip('/').split('/')[-1]}",
                       "title": j.get("title", ""), "location": j.get("locationsText", ""),
                       "remote": False, "url": f"{site_root}/{site}{path}",
                       "posted": (now - timedelta(days=days)).isoformat() if days is not None else None,
                       "ats": "workday"}
            offset += 20
            time.sleep(0.7)
            if len(posts) < 20:
                break


# The next three follow Steve's job-feed (github.com/Passarellism/job-feed), which reads them daily.
def workable(b):
    d = fetch(b.get("feed") or f"https://apply.workable.com/api/v1/widget/accounts/{b['token']}?details=true")
    for j in d.get("jobs", []):
        locs = [", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)]
        locs += [", ".join(x for x in [l.get("city"), l.get("region"), l.get("country")] if x)
                 for l in (j.get("locations") or [])]
        jid = j.get("shortcode") or j.get("id")
        yield {"key": f"wk:{b['token']}:{jid}", "title": j.get("title", ""),
               "location": "; ".join(x for x in dict.fromkeys(locs) if x), "remote": bool(j.get("telecommuting")),
               "url": j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{b['token']}/j/{jid}/",
               "posted": j.get("published_on") or j.get("created_at"), "ats": "workable",
               "description": strip_html(j.get("description", ""))}


def recruitee(b):
    d = fetch(b.get("feed") or f"https://{b['token']}.recruitee.com/api/offers/")
    for j in d.get("offers", []):
        yield {"key": f"rc:{b['token']}:{j.get('id')}", "title": j.get("title", ""),
               "location": j.get("location", "") or "", "remote": bool(j.get("remote")),
               "url": j.get("careers_url"), "posted": j.get("published_at") or j.get("created_at"),
               "ats": "recruitee", "description": strip_html(j.get("description", ""))}


def rippling(b):
    d = fetch(b.get("feed") or f"https://ats.rippling.com/api/v2/board/{b['token']}/jobs")
    items = d if isinstance(d, list) else (d.get("items") or d.get("results") or d.get("jobs") or [])
    for j in items:
        locs = j.get("locations") or j.get("workLocations") or []
        loc = "; ".join((l.get("name") if isinstance(l, dict) else str(l)) for l in locs) or str(j.get("location") or "")
        jid = j.get("id") or j.get("uuid")
        yield {"key": f"rp:{b['token']}:{jid}", "title": j.get("name") or j.get("title") or "",
               "location": loc, "remote": "remote" in loc.lower(),
               "url": j.get("url") or f"https://ats.rippling.com/{b['token']}/jobs/{jid}",
               "posted": j.get("createdOn") or j.get("created_at"), "ats": "rippling",
               "description": strip_html(j.get("description", ""))}


READERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
           "workday": workday, "workable": workable, "recruitee": recruitee, "rippling": rippling}

# ---------- notes from the description: information only, never used to drop a job ----------
YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*years?", re.I)
COMP_RE = re.compile(r"\$\s?\d{2,3}(?:,\d{3})+(?:\s*(?:-|–|to)\s*\$\s?\d{2,3}(?:,\d{3})+)?|\$\s?\d{2,3}\s?[kK](?:\s*(?:-|–|to)\s*\$\s?\d{2,3}\s?[kK])?")


def info_notes(j):
    d = j.get("description") or ""
    notes = []
    ys = [int(m.group(1)) for m in YEARS_RE.finditer(d) if 0 < int(m.group(1)) <= 15]
    if ys and min(ys) >= 3:
        notes.append(f"asks {min(ys)}+ yrs")
    comp = j.get("comp") or (COMP_RE.search(d).group(0) if COMP_RE.search(d) else "")
    if comp:
        notes.append(f"pay {comp.strip()}")
    if re.search(r"active (ts|top secret|secret)|ts/sci", d, re.I):
        notes.append("mentions active clearance")
    return " | ".join(notes)


# ---------- filters, built from config.yaml ----------

def _words(words):
    return re.compile(r"\b(" + "|".join(words) + r")\b", re.I) if words else None


class Filters:
    def __init__(self, cfg):
        f = cfg["filters"]
        self.title_in = _words(f["title_include"])
        self.title_out = _words(f["title_exclude"])
        self.need_signal = _words(f.get("engineer_needs_one_of", []))
        self.always_ok = _words(f.get("titles_ok_without_signal", []))
        self.places = _words(f["places"])
        self.state = re.compile(r",\s*(%s)\b" % "|".join(f.get("state_codes", ["MA"])))
        self.remote_ok = f.get("remote_us_ok", True)
        self.non_us = _words(f.get("non_us_words", []))
        bad = f.get("company_exclude", [])
        self.company_out = re.compile("|".join(re.escape(c) for c in bad), re.I) if bad else None

    def company_ok(self, c):
        return not (self.company_out and self.company_out.search(c or ""))

    def title_ok(self, t):
        t = t or ""
        if not self.title_in.search(t) or (self.title_out and self.title_out.search(t)):
            return False
        if self.always_ok and self.always_ok.search(t):
            return True
        return not self.need_signal or bool(self.need_signal.search(t))

    def location_ok(self, loc, remote=False):
        loc = loc or ""
        if self.places.search(loc) or self.state.search(loc):
            return True
        if not self.remote_ok:
            return False
        if remote or re.search(r"remote|anywhere|distributed", loc, re.I):
            us = re.search(r"\b(us|u\.s\.|usa|united states|north america)\b", loc, re.I)
            return not (self.non_us and self.non_us.search(loc) and not us)
        return bool(re.fullmatch(r"\s*(united states|usa|us)\s*", loc, re.I))


def scan(boards, cfg, seen, days, log=print):
    """Return new matching jobs from every board. `seen` is a set of keys to skip."""
    flt = Filters(cfg)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    found, merged = [], {}
    def pull(b):
        reader = READERS.get(str(b.get("ats", "")).lower())
        if not reader:
            return b, None, f"unknown ATS {b.get('ats')}"
        try:
            return b, list(reader(b)), None
        except Exception as e:
            return b, None, f"{type(e).__name__}: {str(e)[:100]}"

    # Workday rate-limits per company: a few at a time, and a limited company is skipped, not waited on
    wd = [b for b in boards if str(b.get("ats", "")).lower() == "workday"]
    other = [b for b in boards if b not in wd]
    log(f"Checking {len(boards)} company boards ({len(wd)} on Workday)...")
    ex1, ex2 = ThreadPoolExecutor(12), ThreadPoolExecutor(4)
    futures = {ex1.submit(pull, b): b for b in other}
    futures.update({ex2.submit(pull, b): b for b in wd})
    results, pending, last_note = [], set(futures), time.time()
    try:
        while pending:   # short waits so Ctrl+C works, and a progress line so it never looks frozen
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            results += [f.result() for f in done]
            if time.time() - last_note > 20 and pending:
                names = ", ".join((futures[f].get("company") or "?") for f in list(pending)[:3])
                log(f"     ...{len(results)}/{len(boards)} boards done, still waiting on: {names}")
                last_note = time.time()
    except KeyboardInterrupt:
        log("\nStopped.")
        ex1.shutdown(wait=False, cancel_futures=True)
        ex2.shutdown(wait=False, cancel_futures=True)
        sys.stdout.flush()
        os._exit(130)
    ex1.shutdown(wait=False)
    ex2.shutdown(wait=False)
    order = {id(b): i for i, b in enumerate(boards)}
    results.sort(key=lambda r: order.get(id(r[0]), 0))

    for b, listing, err in results:
        name = b.get("company") or b.get("token")
        if not flt.company_ok(name):
            continue
        if err:
            log(f"SKIP {name}: Workday is rate limiting, will check again next run" if "SKIP:" in err
                else f"FAIL {name}: {err}")
            continue
        total = kept = 0
        try:
            for j in listing:
                total += 1
                if j["key"] in seen:
                    continue
                if not flt.title_ok(j["title"]) or not flt.location_ok(j["location"], j["remote"]):
                    continue
                dt = parse_dt(j["posted"])
                if dt and dt < cutoff:
                    continue
                if j.get("description") and not (flt.places.search(j["location"] or "") or flt.state.search(j["location"] or "")):
                    from .sources import remote_conflict      # kept only because it's remote: verify that
                    if remote_conflict(j["description"], flt):
                        continue
                j["notes"] = info_notes(j)
                j.pop("description", None)
                j["company"] = name
                j["posted"] = dt.date().isoformat() if dt else None
                # the same title posted for several offices becomes one job
                k = (name.lower(), re.sub(r"\s+", " ", j["title"].strip().lower()))
                if k in merged:
                    m = merged[k]
                    m["alt_keys"].append(j["key"])
                    if j["location"] not in m["location"]:
                        m["location"] += "; " + j["location"]
                    continue
                j["alt_keys"] = []
                merged[k] = j
                found.append(j)
                kept += 1
            log(f"OK   {name}: {total} listed, {kept} new")
        except Exception as e:
            log(f"FAIL {name}: {type(e).__name__}: {str(e)[:100]}")
    return found


def check(ats, token, feed=None):
    """Count jobs on one board; raises if the token is wrong."""
    b = {"company": token, "ats": ats, "token": token}
    if feed:
        b["feed"] = feed
    return sum(1 for _ in READERS[ats.lower()](b))
