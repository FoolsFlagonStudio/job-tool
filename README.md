# job-tool

Finds early-career software jobs and prefills the applications. **You review each one and click Submit yourself.**
It runs on your own machine and uses no AI, so it costs nothing to run.

- `find` checks ~95 company job boards (Greenhouse, Lever, Ashby, Workday, SmartRecruiters) through their public job feeds, Built In Boston, LinkedIn's public (logged-out) job search, and any links you paste into `urls.txt`. It keeps only jobs that match your title and location filters and skips anything it has already seen.
- `apply` opens the next few jobs in Chrome, fills every field it can answer from `profile.yaml`, uploads the best-matching resume, and outlines in red the required fields it couldn't answer. It **never** clicks Submit. When a confirmation page shows up, it marks that job submitted.

## Setup (once)

You need Python 3.10+ and Google Chrome.

**Windows (PowerShell)**
```powershell
git clone <repo-url> job-tool
cd job-tool
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.yaml config.yaml
copy profile.example.yaml profile.yaml
copy urls.example.txt urls.txt
```

**Mac (Terminal)**
```bash
git clone <repo-url> job-tool
cd job-tool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp profile.example.yaml profile.yaml
cp urls.example.txt urls.txt
```

No Chrome? Run `playwright install chromium` and set `apply: browser: chromium` in `config.yaml`.

Then:
1. Fill in `profile.yaml` with your answers.
2. Put your resume PDFs in `resumes/<variant>/` (see `resumes/README.md`). The variants and the keywords that pick each one are in `config.yaml`. One variant is enough: delete the others from the config.
3. Edit `filters` and `boards` in `config.yaml` for your search.

`config.yaml`, `profile.yaml`, `urls.txt`, your resume PDFs and `data/` are git-ignored, so your personal info never gets committed.

## Daily use

Activate the venv first (`.venv\Scripts\Activate.ps1` on Windows, `source .venv/bin/activate` on Mac).

```bash
python -m jobtool find               # pull new matches from every source (a couple of minutes)
python -m jobtool find --only boards linkedin   # just some sources: boards builtin hiringcafe wellfound linkedin urls
python -m jobtool list               # see the queue
python -m jobtool apply              # open and prefill the next 5 (batch_size in config)
python -m jobtool apply --count 10
python -m jobtool apply --ids 12 15  # specific jobs
python -m jobtool apply --marathon   # days off: keep 5 tabs filled, refilling as you submit or close each one
python -m jobtool apply --marathon --count 3 --workday   # 3 tabs at a time, Workday jobs included
python -m jobtool list --easy        # LinkedIn Easy Apply jobs only   (--forms for everything else)
python -m jobtool apply --easy       # work through Easy Apply jobs, 5 tabs at a time
python -m jobtool list --status filled --notes   # what each prefilled job still needs
python -m jobtool applied           # what you've applied to since last time: date, company, title, link
python -m jobtool applied --all     # every application
python -m jobtool forget --today     # un-see everything found today (keeps submitted ones), then find again
python -m jobtool mark 12 skipped    # statuses: new filled submitted skipped needs_you closed
python -m jobtool check greenhouse cargurus      # test a board token before adding it
```

During `apply`, each job gets its own tab, and the terminal prints `READY` when a tab is done filling.
Wait for READY before typing in that tab, so you and the filler aren't typing in the same tab at once. Fix anything outlined in red, answer the essay questions, check the
resume, and click Submit. Close the browser when you're done.

### Notes under each job
For boards whose feed includes the job description (Greenhouse, Lever, Ashby, Workable, Recruitee,
Rippling), `list` shows a short line under new jobs, e.g. `asks 3+ yrs | pay $116,000`. These notes are for
information only and never hide a job. The description is also used to drop "remote" jobs that turn out to be
on-site, hybrid or relocating outside your area, the same check LinkedIn jobs get.

### Closed and reposted jobs
When a posting says it's no longer accepting applications, the job is marked `closed`. If the same company
later posts the same title again (a new listing or a repost), `find` puts it back in the queue with a
`REPOSTED` note instead of treating it as a duplicate. Skipped and submitted jobs stay out.

### Tracking what you applied to
When a confirmation page appears after you click Submit, the tool marks that job submitted and saves the date.
If it misses one, run `python -m jobtool mark <id> submitted`.

`python -m jobtool applied` prints every application you haven't copied over yet, one tab-separated row each
(Date applied, Company, Job title, Location, Link, Resume). Select the rows and paste them into a spreadsheet
or tracker table, and each value lands in its own column. Those rows also get added to `applied.csv`, a running log you can open in
Excel. Each application shows up only once, so you never copy the same one twice. `--all` shows everything again,
and `--since 2026-11-01` limits it by date.

Jobs you paste into `urls.txt` get their real job title from the posting page the first time `apply` opens them.

### Recruiter outreach
When a LinkedIn job page shows "Meet the hiring team" or a job poster, the tool saves that person's name and
profile link. `list --notes` shows it, and `applied` puts it in a "Recruiter / who to contact" column.
Jobs without one get a ready-made LinkedIn search link for "<company> technical recruiter" instead, so after
applying you can open it and message someone. The tool never sends messages.

### Referrals from your LinkedIn connections
Download your connections from LinkedIn: Settings > Data privacy > Get a copy of your data > Connections.
LinkedIn emails a link, usually within minutes. Put `Connections.csv` in the job-tool folder (it's git-ignored).
`find` then tags every job at a company where you know someone with
`REFERRAL: 2 connections - Jane Doe (Software Engineer), ...` and saves their profile links in the
"who to contact" column of `applied`. `python -m jobtool list --referrals` shows just those jobs. Asking a
connection for a referral before applying is usually the highest-odds move. Re-download the file now and then,
since people change jobs.

### Blocked companies
`filters: company_exclude:` in `config.yaml` lists companies to ignore everywhere: job-reposting sites
(MeeBoss, RemoteHunter, Jobgether...), AI-training gig sites, and "training then placement" programs that
bind you to a contract (BeaconFire, SynergisticIT, Revature...). Add or remove names as you run into them.

### Sign-ins
The tool uses its own Chrome profile (`data/browser-profile`), **not** your everyday Chrome, so being logged in there doesn't count.
- Greenhouse, Lever and Ashby need no account.
- Workday needs a separate account per company. The first time, sign in or create the account yourself in the tool's window. It stays signed in after that.
- If a job hits a sign-in wall, the tool marks it `needs_you` and leaves the tab open. Sign in there, then run `python -m jobtool apply --ids <id>` again.

## Adding companies

Easiest: `python -m jobtool add "Acme" "Beta Inc"`. It checks Greenhouse, Ashby, Lever, Workable,
SmartRecruiters, Recruitee and Rippling for a board that belongs to that company (picking the busiest one if it
has several) and adds it to `config.yaml`. You can also just put `- {company: "Acme"}` under `boards:`;
`find` looks it up the first time and remembers it (no-match companies are retried weekly).

If that finds nothing, the company is probably on Workday or a custom site. Look at its job links:

| Link looks like | Add to `boards:` |
|---|---|
| `job-boards.greenhouse.io/acme/...` | `{company: Acme, ats: greenhouse, token: acme}` |
| `jobs.lever.co/acme/...` | `{company: Acme, ats: lever, token: acme}` |
| `jobs.ashbyhq.com/acme/...` | `{company: Acme, ats: ashby, token: acme}` |
| `acme.wd5.myworkdayjobs.com/External` | `{company: Acme, ats: workday, token: acme, feed: "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"}` |
| `apply.workable.com/acme/...` | `{company: Acme, ats: workable, token: acme}` |
| `acme.recruitee.com/...` | `{company: Acme, ats: recruitee, token: acme}` |
| `ats.rippling.com/acme/jobs/...` | `{company: Acme, ats: rippling, token: acme}` |

Check it with `python -m jobtool check <ats> <token>`.

## LinkedIn, Built In, Indeed

- **LinkedIn**: `find` reads LinkedIn's public job search, the version anyone sees when logged out, so your
  account is never used. Edit the searches under `sources: linkedin:` in `config.yaml`. `past: auto` (the default) searches back
  to your last LinkedIn run, so days away aren't missed. New LinkedIn jobs show as `[LI?]` in `list`. `apply --easy` opens them while
  you're signed in, keeps the real Easy Apply ones (`[EASY]`), and moves company-site ones to the forms queue
  for `apply --marathon`. This saves one LinkedIn request per job, which means less throttling. If LinkedIn throttles you (FAIL ... 429), wait an hour or raise `pause_seconds`.
- **Easy Apply sessions**: `python -m jobtool apply --easy` opens the Easy Apply jobs 5 tabs at a time,
  newest first. Sign in to LinkedIn in that window the first time; it stays signed in. You click Easy Apply
  and submit. LinkedIn fills in most of it from your profile, and the tool never clicks anything on LinkedIn.
  Some jobs look like Easy Apply from the logged-out search but actually apply on the company's site. The
  session spots the real button and moves those to the forms queue (`apply --marathon` prefills them).
  When LinkedIn shows "application was sent" or "Applied", the tool marks the job submitted, closes the tab
  and opens the next one. Jobs you applied to before are skipped automatically. Upload your resume variants
  to LinkedIn once (Settings > Data privacy > Job application settings) so you can pick one per job.
- **"Remote" that isn't**: LinkedIn's remote filter lets through lots of on-site and hybrid jobs. A job that
  only qualifies because it's remote has its description checked during `find` and again when its tab opens.
  The job is dropped only if the description puts you on-site, hybrid or relocating somewhere outside your `places` list. On-site in Boston or relocating to Cambridge is fine.
- **Throttling (429)**: the tool now waits and retries, and if LinkedIn is still throttling it skips the rest
  of the LinkedIn searches for that run and keeps what it found. Run `find --only linkedin` again an hour later.
  Raise `pause_seconds` if it keeps happening.
- **Built In Boston**: `find` opens the entry-level, junior and mid-level engineering listings in a hidden
  browser. During `apply` the tool clicks Built In's Apply button and fills the company's own form. If Built In
  starts showing a "verify you are human" page, set `sources: builtin: headless: false`.
- **hiring.cafe**: collects jobs straight from company career pages. `find` opens each search URL under
  `sources: hiringcafe: searches:` in a hidden browser and reads the jobs the page loads. Easiest way to tune it:
  build a search on hiring.cafe (location, seniority, date), copy the address bar URL, paste it into the list.
- **Wellfound**: needs you logged in, so `find` opens a visible window. The first time, log in there (it
  remembers you). It reads the jobs Wellfound shows for your profile's role and location preferences.
  Wellfound jobs are applied to on Wellfound, so they show as `[WF]` and come up in `apply --easy` alongside
  LinkedIn Easy Apply jobs. If a Wellfound tab doesn't get marked after you apply, `mark <id> submitted`.
- **Indeed** blocks automated browsers, so copy any Indeed links you want into `urls.txt`.

Anything in `urls.txt` (one link per line) joins the same queue and is deduped like everything else.

## Notes

- Workday jobs are found but skipped by `apply` unless you pass `--ids`, because every Workday site needs an account.
- If a form gets something wrong, change or add a rule in `profile.yaml` under `answers:`. Each rule is a
  regex tested against the field's label, and the first match wins.
