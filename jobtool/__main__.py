"""job-tool: find early-career jobs and prefill applications for you to review and submit.

  python -m jobtool find                 # company boards, Built In, LinkedIn, urls.txt -> save new matches
  python -m jobtool find --only boards   # just one source: boards | builtin | linkedin | urls
  python -m jobtool list [--status new]  # show the queue
  python -m jobtool apply [--count 5]    # open and prefill the next N, you review and submit
  python -m jobtool apply --ids 12 15    # prefill specific jobs
  python -m jobtool apply --marathon     # keep 5 tabs filled, opening the next as you submit or close one
  python -m jobtool apply --easy         # LinkedIn Easy Apply jobs: opens them 5 at a time, you click Easy Apply
  python -m jobtool applied             # applications not yet copied to your tracker (then marks them copied)
  python -m jobtool applied --all       # every application, including ones already copied
  python -m jobtool forget --today       # un-see everything found today (keeps what you submitted), then find again
  python -m jobtool add "Acme" "Beta Inc"  # find each company's job board and add it to config.yaml
  python -m jobtool list --referrals     # jobs at companies where you have LinkedIn connections
  python -m jobtool mark 12 submitted    # fix a status by hand (new, filled, submitted, skipped, needs_you, closed)
  python -m jobtool check greenhouse cargurus   # verify a company board token before adding it
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

from . import boards as B
from .db import DB, STATUSES

BASE = Path(__file__).resolve().parent.parent


def load_yaml(name):
    p = BASE / name
    if not p.exists():
        sys.exit(f"Missing {name}. Copy {name.replace('.yaml', '.example.yaml')} to {name} and fill it in.")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def ats_from_url(url):
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    for key, ats in (("greenhouse", "greenhouse"), ("lever.co", "lever"), ("ashbyhq", "ashby"), ("workable", "workable"), ("recruitee", "recruitee"), ("rippling", "rippling"),
                     ("myworkdayjobs", "workday"), ("smartrecruiters", "smartrecruiters")):
        if key in host:
            return ats
    return "other"


def company_from_url(url):
    parts = re.sub(r"^https?://", "", url).split("/")
    host = parts[0]
    if any(k in host for k in ("greenhouse", "lever.co", "ashbyhq")) and len(parts) > 1:
        return parts[1]
    return host.replace("www.", "").split(".")[0]


def cmd_find(args, cfg, db):
    from . import sources as S
    seen = db.seen_keys()
    days = args.days or cfg.get("lookback_days", 30)
    want = lambda name: not args.only or name in args.only
    jobs = []
    if want("boards"):
        boards = B.resolve_boards([dict(b) for b in cfg.get("boards", [])], BASE / "data" / "detected_boards.json")
        # Workday rate-limits hard and its listings change slowly: check it at most every N hours
        stamp = BASE / "data" / "last_workday_check.txt"
        every = cfg.get("workday_every_hours", 6)
        import time as _t
        recent = stamp.exists() and _t.time() - stamp.stat().st_mtime < every * 3600
        if recent and not args.workday:
            n = sum(1 for b in boards if str(b.get("ats")).lower() == "workday")
            boards = [b for b in boards if str(b.get("ats")).lower() != "workday"]
            if n:
                print(f"(Skipping {n} Workday companies: checked less than {every}h ago. `find --workday` forces it.)")
        jobs += B.scan(boards, cfg, seen, days)
        if not recent or args.workday:
            if not B.WORKDAY_429["gave_up"]:
                stamp.parent.mkdir(exist_ok=True)
                stamp.write_text("ok")
    if want("builtin"):
        jobs += S.builtin(cfg, seen, days, BASE)
    if want("hiringcafe"):
        jobs += S.hiringcafe(cfg, seen, days, BASE)
    if want("wellfound"):
        jobs += S.wellfound(cfg, seen, days, BASE)
    if want("linkedin"):
        jobs += S.linkedin(cfg, seen)
    from . import connections as C
    conns = C.load(BASE)
    added, easy, refs = 0, 0, 0
    for j in jobs:
        if db.same_role_exists(j["company"], j["title"]):
            continue
        people = C.at(conns, j["company"])
        if people:
            j["notes"] = " | ".join(x for x in (C.summary(people), j.get("notes")) if x)
            j["recruiter"] = "; ".join(f"{p['name']} {p['url']}" for p in people[:3])
            refs += 1
        if db.add(j):
            added += 1
            easy += j["ats"] == "linkedin_easy"
    if not want("urls"):
        print(f"\n{added} new job(s) added.")
        return
    # links pasted into urls.txt
    up = BASE / cfg.get("urls_file", "urls.txt")
    if up.exists():
        for line in up.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            clean = url.split("?")[0].rstrip("/")
            if db.known_url(url) or f"url:{clean}" in seen:
                continue
            added += db.add({"key": f"url:{clean}", "company": company_from_url(url),
                             "title": "(from urls.txt)", "location": "", "url": url,
                             "ats": ats_from_url(url), "posted": None})
    print(f"\n{added} new job(s) added. Run `python -m jobtool list` to see them.")
    if refs:
        print(f"{refs} of them are at companies where you have LinkedIn connections: `python -m jobtool list --referrals`")
    if easy:
        print(f"{easy} of them are LinkedIn Easy Apply only: `python -m jobtool list --status new` marks them [EASY].")


def cmd_list(args, cfg, db):
    is_blocked = blocked(cfg)
    rows = [r for r in db.jobs(status=args.status) if not is_blocked(r)]
    if args.referrals:
        rows = [r for r in rows if "REFERRAL" in (r["notes"] or "")]
    if args.easy:
        rows = [r for r in rows if r["ats"] in ("linkedin_easy", "wellfound")]
    elif args.forms:
        rows = [r for r in rows if r["ats"] not in ("linkedin_easy", "wellfound")]
    rows = rows[:args.limit]
    if not rows:
        print("Nothing here.")
        return
    for r in rows:
        loc = (r["location"] or "")[:28]
        tag = {"linkedin_easy": "[EASY] ", "workday": "[WD] ", "builtin": "[BI] ", "linkedin": "[LI] ",
               "wellfound": "[WF] "}.get(r["ats"], "")
        print(f"{r['id']:>4}  {r['status']:<10} {r['posted'] or r['first_seen']}  {tag}"
              f"{(r['company'] or '')[:22]:<22} {(r['title'] or '')[:52]:<52} {loc}")
        if not args.notes and r["status"] == "new" and r["notes"]:
            print(f"      {r['notes']}")
        if args.notes and r["notes"]:
            print(f"      {r['notes']}")
        if args.notes and r["recruiter"]:
            print(f"      Recruiter: {r['recruiter']}")
    print(f"\n{len(rows)} shown.")


def blocked(cfg):
    flt = B.Filters(cfg)
    return lambda r: not flt.company_ok(r["company"])


def cmd_apply(args, cfg, db):
    from . import fill
    is_blocked = blocked(cfg)
    if args.easy:
        jobs = db.jobs(ids=args.ids) if args.ids else \
            [j for j in db.jobs(status="new") if j["ats"] in ("linkedin_easy", "wellfound") and not is_blocked(j)]
        if not jobs:
            print("No Easy Apply jobs waiting. Run `python -m jobtool find` first.")
            return
        print(f"{len(jobs)} Easy Apply job(s), opening {args.count or 5} at a time...")
        fill.run_easy(db, cfg, BASE, jobs, keep_open=args.count or 5)
        return
    profile = load_yaml("profile.yaml")
    if args.ids:
        jobs = db.jobs(ids=args.ids)
    else:
        skip = {"linkedin_easy", "wellfound"} | (set() if args.workday else {"workday"})
        jobs = [j for j in db.jobs(status="new") if j["ats"] not in skip and not is_blocked(j)]
        if not args.marathon:
            jobs = jobs[:args.count or cfg["apply"]["batch_size"]]
    if not jobs:
        print("No new jobs to apply to. Run `python -m jobtool find` first.")
        return
    print(f"Opening {len(jobs)} application(s)...")
    keep = (args.count or cfg["apply"]["batch_size"]) if args.marathon else None
    fill.run(db, cfg, profile, BASE, jobs, headless=args.headless, watch=not args.no_watch, keep_open=keep)


def cmd_applied(args, cfg, db):
    import csv
    rows = db.applied(include_exported=args.all, since=args.since)
    if not rows:
        print("No new applications to copy over." if not args.all else "No submitted applications yet.")
        return
    out = BASE / "applied.csv"
    if out.exists() and "Recruiter" not in out.read_text(encoding="utf-8").split("\n", 1)[0]:
        out.rename(BASE / "applied-old.csv")      # older layout without the recruiter column
    new_file = not out.exists()
    from urllib.parse import quote
    header = ["Date applied", "Company", "Job title", "Location", "Link", "Resume", "Recruiter / who to contact"]

    def contact(r):
        if r["recruiter"]:
            return r["recruiter"]
        return ("Search: https://www.linkedin.com/search/results/people/?keywords="
                + quote(f'{r["company"]} technical recruiter'))
    lines = [[r["submitted_at"] or r["updated"], r["company"], r["title"], r["location"] or "",
              r["url"], r["resume"] or ("LinkedIn profile" if r["ats"] == "linkedin_easy" else ""), contact(r)]
             for r in rows]
    print("\t".join(header))
    for l in lines:
        print("\t".join(l))
    if not args.all:
        with open(out, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(header)
            w.writerows(lines)
        db.mark_exported([r["id"] for r in rows])
        print(f"\n{len(rows)} application(s) above. Tab-separated, so they paste straight into a sheet or table.")
        print(f"Also added to {out.name}. They won't show here again (use --all to see everything).")


def cmd_forget(args, cfg, db):
    from datetime import date
    since = date.today().isoformat() if args.today else args.since
    if not since:
        sys.exit("Use --today or --since YYYY-MM-DD")
    n = db.forget(since)
    print(f"Forgot {n} job(s) first seen since {since} (submitted ones kept). Run `python -m jobtool find` for a fresh pull.")


def cmd_add(args, cfg, db):
    """Find each company's job board and append it to config.yaml."""
    path = BASE / "config.yaml"
    have = {B.norm_name(b.get("company")) for b in cfg.get("boards", [])}
    lines = []
    for name in args.names:
        if B.norm_name(name) in have:
            print(f"{name}: already in config.yaml")
            continue
        res = B.detect(name)
        if not res:
            print(f"{name}: no Greenhouse/Ashby/Lever/Workable/SmartRecruiters/Recruitee/Rippling board found."
                  " If its job links go to myworkdayjobs.com, add it as a Workday board (see README).")
            continue
        ats, token, n = res
        print(f"{name}: {ats} '{token}' - {n} open jobs. Added.")
        lines.append(f'  - {{company: "{name}", ats: {ats}, token: {token}}}')
    if lines:
        text = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_text(text + "\n" + "\n".join(lines) + "\n", encoding="utf-8")


def cmd_mark(args, cfg, db):
    db.set(args.id, status=args.status)
    print(f"Job {args.id} -> {args.status}")


def cmd_check(args, cfg, db):
    try:
        n = B.check(args.ats, args.token, args.feed)
        print(f"OK: {args.ats} board '{args.token}' has {n} jobs")
    except Exception as e:
        sys.exit(f"Not found or not reachable: {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser(prog="jobtool", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find"); f.add_argument("--days", type=int)
    f.add_argument("--only", nargs="+", choices=["boards", "builtin", "hiringcafe", "wellfound", "linkedin", "urls"])
    f.add_argument("--workday", action="store_true", help="check Workday even if it was checked recently")
    l = sub.add_parser("list"); l.add_argument("--status", choices=STATUSES); l.add_argument("--limit", type=int, default=60)
    l.add_argument("--notes", action="store_true", help="show what each filled job still needs")
    l.add_argument("--easy", action="store_true", help="only LinkedIn Easy Apply jobs")
    l.add_argument("--referrals", action="store_true", help="only jobs where you have a LinkedIn connection")
    l.add_argument("--forms", action="store_true", help="only jobs with a real application form")
    a = sub.add_parser("apply"); a.add_argument("--count", type=int); a.add_argument("--ids", type=int, nargs="+")
    a.add_argument("--marathon", action="store_true",
                   help="work through the whole queue, keeping --count tabs filled at a time")
    a.add_argument("--workday", action="store_true", help="include Workday jobs (each site needs an account)")
    a.add_argument("--easy", action="store_true", help="LinkedIn Easy Apply jobs only (you click Easy Apply)")
    a.add_argument("--headless", action="store_true", help="testing only: no visible browser")
    a.add_argument("--no-watch", action="store_true", help="don't wait for you to submit")
    ap_ = sub.add_parser("applied"); ap_.add_argument("--all", action="store_true")
    ap_.add_argument("--since", help="only applications on or after this date, YYYY-MM-DD")
    fg = sub.add_parser("forget"); fg.add_argument("--today", action="store_true"); fg.add_argument("--since")
    ad = sub.add_parser("add"); ad.add_argument("names", nargs="+", help='company names, e.g. "Wayfair" "Rapid7"')
    m = sub.add_parser("mark"); m.add_argument("id", type=int); m.add_argument("status", choices=STATUSES)
    c = sub.add_parser("check"); c.add_argument("ats"); c.add_argument("token"); c.add_argument("feed", nargs="?")
    args = ap.parse_args()

    if args.cmd == "check":
        return cmd_check(args, None, None)
    cfg = load_yaml("config.yaml")
    db = DB(BASE / cfg.get("database", "data/jobs.db"))
    {"find": cmd_find, "list": cmd_list, "applied": cmd_applied, "forget": cmd_forget, "add": cmd_add, "apply": cmd_apply, "mark": cmd_mark}[args.cmd](args, cfg, db)


if __name__ == "__main__":
    main()
