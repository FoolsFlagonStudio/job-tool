"""Your LinkedIn connections, from LinkedIn's own data export (no scraping, no sign-in needed).

Get it: LinkedIn > Settings > Data privacy > Get a copy of your data > Connections. LinkedIn emails a
download link (usually within minutes); put Connections.csv in the job-tool folder (or data/)."""
import csv
import io
from pathlib import Path

from .boards import norm_name


def load(base):
    for p in (Path(base) / "Connections.csv", Path(base) / "data" / "Connections.csv"):
        if p.exists():
            break
    else:
        return {}
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("First Name")), 0)   # skip LinkedIn's notes
    by_company = {}
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        co = norm_name(r.get("Company"))
        if not co:
            continue
        name = f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip()
        by_company.setdefault(co, []).append({"name": name, "position": r.get("Position", ""), "url": r.get("URL", "")})
    return by_company


def at(conns, company):
    return conns.get(norm_name(company), [])


def summary(people, limit=3):
    if not people:
        return ""
    names = ", ".join(f"{p['name']} ({p['position']})" if p["position"] else p["name"] for p in people[:limit])
    more = f" +{len(people) - limit} more" if len(people) > limit else ""
    return f"REFERRAL: {len(people)} connection{'s' if len(people) > 1 else ''} - {names}{more}"
