"""job-tool: find early-career jobs and prefill applications for you to review and submit.

  python -m jobtool find                 # check company boards + urls.txt, save new matches
  python -m jobtool list [--status new]  # show the queue
  python -m jobtool apply [--count 5]    # open and prefill the next N, you review and submit
  python -m jobtool apply --ids 12 15    # prefill specific jobs
  python -m jobtool applied             # applications not yet copied to your tracker (then marks them copied)
  python -m jobtool applied --all       # every application, including ones already copied
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
    for key, ats in (("greenhouse", "greenhouse"), ("lever.co", "lever"), ("ashbyhq", "ashby"),
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
    seen = db.seen_keys()
    added = 0
    jobs = B.scan(cfg.get("boards", []), cfg, seen, args.days or cfg.get("lookback_days", 30))
    for j in jobs:
        dup = db.same_role_exists(j["company"], j["title"])
        if dup:
            continue
        added += db.add(j)
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


def cmd_list(args, cfg, db):
    rows = db.jobs(status=args.status, limit=args.limit)
    if not rows:
        print("Nothing here.")
        return
    for r in rows:
        loc = (r["location"] or "")[:28]
        print(f"{r['id']:>4}  {r['status']:<10} {r['posted'] or r['first_seen']}  "
              f"{(r['company'] or '')[:22]:<22} {(r['title'] or '')[:52]:<52} {loc}")
        if args.notes and r["notes"]:
            print(f"      {r['notes']}")
    print(f"\n{len(rows)} shown.")


def cmd_apply(args, cfg, db):
    from . import fill
    profile = load_yaml("profile.yaml")
    jobs = db.jobs(ids=args.ids) if args.ids else db.jobs(status="new", limit=args.count or cfg["apply"]["batch_size"])
    jobs = [j for j in jobs if j["ats"] != "workday"] if not args.ids else jobs
    if not jobs:
        print("No new jobs to apply to. Run `python -m jobtool find` first.")
        return
    print(f"Opening {len(jobs)} application(s)...")
    fill.run(db, cfg, profile, BASE, jobs, headless=args.headless, watch=not args.no_watch)


def cmd_applied(args, cfg, db):
    import csv
    rows = db.applied(include_exported=args.all, since=args.since)
    if not rows:
        print("No new applications to copy over." if not args.all else "No submitted applications yet.")
        return
    out = BASE / "applied.csv"
    new_file = not out.exists()
    header = ["Date applied", "Company", "Job title", "Location", "Link", "Resume"]
    lines = [[r["submitted_at"] or r["updated"], r["company"], r["title"], r["location"] or "",
              r["url"], r["resume"] or ""] for r in rows]
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
    l = sub.add_parser("list"); l.add_argument("--status", choices=STATUSES); l.add_argument("--limit", type=int, default=60)
    l.add_argument("--notes", action="store_true", help="show what each filled job still needs")
    a = sub.add_parser("apply"); a.add_argument("--count", type=int); a.add_argument("--ids", type=int, nargs="+")
    a.add_argument("--headless", action="store_true", help="testing only: no visible browser")
    a.add_argument("--no-watch", action="store_true", help="don't wait for you to submit")
    ap_ = sub.add_parser("applied"); ap_.add_argument("--all", action="store_true")
    ap_.add_argument("--since", help="only applications on or after this date, YYYY-MM-DD")
    m = sub.add_parser("mark"); m.add_argument("id", type=int); m.add_argument("status", choices=STATUSES)
    c = sub.add_parser("check"); c.add_argument("ats"); c.add_argument("token"); c.add_argument("feed", nargs="?")
    args = ap.parse_args()

    if args.cmd == "check":
        return cmd_check(args, None, None)
    cfg = load_yaml("config.yaml")
    db = DB(BASE / cfg.get("database", "data/jobs.db"))
    {"find": cmd_find, "list": cmd_list, "applied": cmd_applied, "apply": cmd_apply, "mark": cmd_mark}[args.cmd](args, cfg, db)


if __name__ == "__main__":
    main()
