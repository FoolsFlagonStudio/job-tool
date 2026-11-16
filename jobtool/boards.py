"""Fetch open jobs from company job boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday)
through their public JSON feeds. No login, no scraping, no AI."""
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (job-tool)", "Accept": "application/json"}


def fetch(url, data=None, timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    headers = dict(UA)
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # retry once
            last = e
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


# ---------- feed readers: each yields dicts with key, title, location, remote, url, posted ----------

def greenhouse(b):
    url = b.get("feed") or f"https://boards-api.greenhouse.io/v1/boards/{b['token']}/jobs"
    for j in fetch(url).get("jobs", []):
        yield {"key": f"gh:{b['token']}:{j['id']}", "title": j.get("title", ""),
               "location": (j.get("location") or {}).get("name", ""), "remote": False,
               "url": j.get("absolute_url"), "posted": j.get("first_published") or j.get("updated_at"),
               "ats": "greenhouse"}


def lever(b):
    url = b.get("feed") or f"https://api.lever.co/v0/postings/{b['token']}?mode=json"
    for j in fetch(url):
        c = j.get("categories") or {}
        locs = c.get("allLocations") or [c.get("location", "")]
        yield {"key": f"lv:{b['token']}:{j['id']}", "title": j.get("text", ""),
               "location": "; ".join(x for x in locs if x),
               "remote": j.get("workplaceType") == "remote", "url": j.get("hostedUrl"),
               "posted": j.get("createdAt"), "ats": "lever"}


def ashby(b):
    url = b.get("feed") or f"https://api.ashbyhq.com/posting-api/job-board/{b['token']}"
    for j in fetch(url).get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
        loc = "; ".join(x for x in locs if x) + (f" ({country})" if country else "")
        yield {"key": f"ab:{b['token']}:{j['id']}", "title": j.get("title", ""), "location": loc,
               "remote": j.get("workplaceType") == "Remote", "url": j.get("jobUrl"),
               "posted": j.get("publishedAt"), "ats": "ashby"}


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


def workday(b):
    """feed = https://<tenant>.wdN.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs"""
    feed = b["feed"]
    site_root = feed.split("/wday/cxs/")[0]
    site = feed.rstrip("/").split("/")[-2]
    now = datetime.now(timezone.utc)
    for search in ("software engineer", "developer"):
        offset = 0
        while offset < 200:
            d = fetch(feed, {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search})
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
            if len(posts) < 20:
                break


READERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
           "smartrecruiters": smartrecruiters, "workday": workday}


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
    for b in boards:
        reader = READERS.get(str(b.get("ats", "")).lower())
        name = b.get("company") or b.get("token")
        if not reader:
            log(f"SKIP {name}: unknown ATS {b.get('ats')}")
            continue
        total = kept = 0
        try:
            for j in reader(b):
                total += 1
                if j["key"] in seen:
                    continue
                if not flt.title_ok(j["title"]) or not flt.location_ok(j["location"], j["remote"]):
                    continue
                dt = parse_dt(j["posted"])
                if dt and dt < cutoff:
                    continue
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
