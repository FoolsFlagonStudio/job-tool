# job-tool

Finds early-career software jobs and prefills the applications. **You review each one and click Submit yourself.**
It runs on your own machine and uses no AI, so it costs nothing to run.

- `find` checks ~40 company job boards (Greenhouse, Lever, Ashby, Workday, SmartRecruiters) through their public job feeds, plus any links you paste into `urls.txt`. It keeps only jobs that match your title and location filters and skips anything it has already seen.
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
python -m jobtool find               # pull new matches (about 10 seconds)
python -m jobtool list               # see the queue
python -m jobtool apply              # open and prefill the next 5 (batch_size in config)
python -m jobtool apply --count 10
python -m jobtool apply --ids 12 15  # specific jobs
python -m jobtool list --status filled --notes   # what each prefilled job still needs
python -m jobtool applied           # what you've applied to since last time: date, company, title, link
python -m jobtool applied --all     # every application
python -m jobtool mark 12 skipped    # statuses: new filled submitted skipped needs_you closed
python -m jobtool check greenhouse cargurus      # test a board token before adding it
```

During `apply`, each job gets its own tab. Fix anything outlined in red, answer the essay questions, check the
resume, and click Submit. Close the browser when you're done.

### Tracking what you applied to
When a confirmation page appears after you click Submit, the tool marks that job submitted and saves the date.
If it misses one, run `python -m jobtool mark <id> submitted`.

`python -m jobtool applied` prints every application you haven't copied over yet, one tab-separated row each
(Date applied, Company, Job title, Location, Link, Resume). Select the rows and paste them into a spreadsheet
or tracker table, and each value lands in its own column. Those rows also get added to `applied.csv`, a running log you can open in
Excel. Each application shows up only once, so you never copy the same one twice. `--all` shows everything again,
and `--since 2026-11-01` limits it by date.

Jobs you paste into `urls.txt` get their real job title from the posting page the first time `apply` opens them.

### Sign-ins
The tool uses its own Chrome profile (`data/browser-profile`), **not** your everyday Chrome, so being logged in there doesn't count.
- Greenhouse, Lever and Ashby need no account.
- Workday needs a separate account per company. The first time, sign in or create the account yourself in the tool's window. It stays signed in after that.
- If a job hits a sign-in wall, the tool marks it `needs_you` and leaves the tab open. Sign in there, then run `python -m jobtool apply --ids <id>` again.

## Adding companies

Find the company's careers page and look at the job links:

| Link looks like | Add to `boards:` |
|---|---|
| `job-boards.greenhouse.io/acme/...` | `{company: Acme, ats: greenhouse, token: acme}` |
| `jobs.lever.co/acme/...` | `{company: Acme, ats: lever, token: acme}` |
| `jobs.ashbyhq.com/acme/...` | `{company: Acme, ats: ashby, token: acme}` |
| `acme.wd5.myworkdayjobs.com/External` | `{company: Acme, ats: workday, token: "acme/wd5/External"}` |

Check it with `python -m jobtool check <ats> <token>`.

## LinkedIn, Built In, Indeed

These have no public feed, and automating a logged-in LinkedIn account can get it restricted. Copy the job links you
like (from alert emails or searches) into `urls.txt`, one per line, and run `find`. They join the same queue
and are deduped like everything else. Links that go to Greenhouse, Lever or Ashby forms get prefilled the
same way. Other sites open with a best-effort fill.

## Notes

- Workday jobs are found but skipped by `apply` unless you pass `--ids`, because every Workday site needs an account.
- If a form gets something wrong, change or add a rule in `profile.yaml` under `answers:`. Each rule is a
  regex tested against the field's label, and the first match wins.
