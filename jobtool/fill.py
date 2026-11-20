"""Open job applications in a real browser and prefill them from profile.yaml.

It NEVER clicks Submit. It fills what it can, highlights required fields it could not
answer, and leaves each tab open for you to review and submit yourself. While you work,
it watches the tabs and marks a job submitted when a confirmation page appears."""
import re
import time
from pathlib import Path

# ---------------------------------------------------------------- field discovery (runs in page)
COLLECT_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').replace(/\*/g, '').trim();
  const labelOf = el => {
    if (el.labels && el.labels.length) return clean([...el.labels].map(l => l.innerText).join(' '));
    if (el.getAttribute('aria-labelledby')) {
      return clean(el.getAttribute('aria-labelledby').split(' ')
        .map(id => (document.getElementById(id) || {}).innerText || '').join(' '));
    }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    // nearest label-ish text above the field inside its container
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
      const lab = p.querySelector('label, legend, .application-label, [class*="label"], [class*="Label"]');
      if (lab && !lab.contains(el) && clean(lab.innerText)) return clean(lab.innerText);
    }
    return clean(el.placeholder || el.name || el.id);
  };
  const out = []; let n = 0;
  const mark = el => { const id = 'jt' + (n++); el.setAttribute('data-jt', id); return id; };
  // text-like inputs, textareas, selects, comboboxes, file inputs
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) return;
    if (type === 'file') { out.push({jt: mark(el), kind: 'file', label: labelOf(el),
      name: el.name || '', id: el.id || '', accept: el.accept || ''}); return; }
    if (!vis(el)) return;
    if (type === 'radio' || type === 'checkbox') return;   // handled as groups below
    if (el.getAttribute('aria-hidden') === 'true' || el.readOnly && el.tabIndex === -1) return;
    const cs = getComputedStyle(el);
    if (parseFloat(cs.opacity) === 0 && el.getAttribute('role') !== 'combobox') return;   // handled as groups below
    const kind = el.tagName === 'SELECT' ? 'select'
      : (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')) ? 'combobox'
      : el.tagName === 'TEXTAREA' ? 'textarea' : 'text';
    out.push({jt: mark(el), kind, label: labelOf(el), name: el.name || '', id: el.id || '',
      type, required: el.required || el.getAttribute('aria-required') === 'true' ||
        /\*/.test((el.labels && el.labels[0] && el.labels[0].innerText) || ''),
      value: el.value || '',
      options: el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()).filter(Boolean) : []});
  });
  // radio and checkbox groups (by name), with the question taken from the fieldset/legend or container
  const groups = {};
  document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach(el => {
    if (!vis(el) && !(el.labels && el.labels[0] && vis(el.labels[0]))) return;
    const key = el.name || el.id; (groups[key] = groups[key] || []).push(el);
  });
  Object.values(groups).forEach(els => {
    const first = els[0];
    let q = '';
    const fs = first.closest('fieldset');
    if (fs && fs.querySelector('legend')) q = clean(fs.querySelector('legend').innerText);
    if (!q) { let p = first.parentElement;
      for (let i = 0; i < 6 && p && !q; i++, p = p.parentElement) {
        const lab = [...p.querySelectorAll('label, legend, [class*="label"], [class*="question"]')]
          .find(l => !els.some(e => l.contains(e) || (e.labels && [...e.labels].includes(l))) && clean(l.innerText));
        if (lab) q = clean(lab.innerText);
      } }
    out.push({kind: first.type === 'radio' ? 'radio' : 'checkbox', label: q,
      required: els.some(e => e.required), checked: els.some(e => e.checked),
      options: els.map(e => ({jt: mark(e), text: labelOf(e)}))});
  });
  // Yes/No button groups (Ashby style)
  document.querySelectorAll('div, fieldset').forEach(box => {
    const btns = [...box.children].filter(c => c.tagName === 'BUTTON' && vis(c));
    if (btns.length < 2 || btns.length > 4) return;
    const texts = btns.map(b => clean(b.innerText).toLowerCase());
    if (!texts.includes('yes') || !texts.includes('no')) return;
    let q = ''; let p = box.parentElement;
    for (let i = 0; i < 4 && p && !q; i++, p = p.parentElement) {
      const lab = p.querySelector('label, legend, [class*="label"]');
      if (lab && !box.contains(lab) && clean(lab.innerText)) q = clean(lab.innerText);
    }
    out.push({kind: 'buttons', label: q, required: false, checked: false,
      options: btns.map(b => ({jt: mark(b), text: clean(b.innerText)}))});
  });
  return out;
}
"""

HIGHLIGHT_JS = """(jt) => { const el = document.querySelector(`[data-jt="${jt}"]`);
  if (el) { el.style.outline = '3px solid #e5484d'; el.style.outlineOffset = '2px'; } }"""

CONFIRM_RE = re.compile(
    r"thank(s| you) for (applying|your application|submitting)|application (has been |was )?"
    r"(received|submitted)|we('ve| have) received your application|successfully submitted", re.I)


# ---------------------------------------------------------------- answers
def window_opts(headless):
    """Visible browser: start maximized and let pages fill the whole window (a fixed viewport cut forms off
    at the bottom). Hidden browser: a fixed size."""
    if headless:
        return {"viewport": {"width": 1280, "height": 900}}
    return {"no_viewport": True, "args": ["--start-maximized"]}


def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def build_rules(profile):
    """Turn profile.yaml into an ordered list of (regex, answer). Specific rules first."""
    p = profile.get("personal", {})
    a = p.get("address", {})
    rules = [
        (r"preferred (first )?name|nickname", p.get("preferred_name") or p.get("first_name")),
        (r"first name|given name|^first$", p.get("first_name")),
        (r"last name|family name|surname|^last$", p.get("last_name")),
        (r"middle name", ""),
        (r"e-?mail", p.get("email")),
        (r"phone|mobile|cell", p.get("phone")),
        (r"linkedin", p.get("linkedin")),
        (r"github", p.get("github")),
        (r"portfolio|personal (web)?site|^website|other website|website url", p.get("website")),
        (r"zip|postal", a.get("zip")),
        (r"street|address line 1|^address", a.get("street")),
        (r"^city$|city of residence", a.get("city")),
        (r"^state|province", a.get("state")),
        (r"^country", a.get("country")),
        (r"location|where are you (currently )?(based|located)|current (city|location)", p.get("location")),
        (r"current (company|employer)|most recent (company|employer)|^company$|^org", p.get("current_company")),
        (r"current (job )?title|most recent (job )?title", p.get("current_title")),
        (r"full name|^name$|your name|legal name", f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()),
    ]
    for r in profile.get("answers", []):
        pats = r["match"] if isinstance(r["match"], list) else [r["match"]]
        for pat in pats:
            rules.append((pat, r["answer"]))
    return [(re.compile(pat, re.I), ans) for pat, ans in rules if ans is not None]


def answer_for(label, rules):
    lab = norm(label)
    if not lab:
        return None
    for rx, ans in rules:
        if rx.search(lab):
            return str(ans)
    return None


def best_option(options, answer):
    """Pick the option text that best matches an answer (exact, then starts-with, then contains)."""
    a = norm(answer)
    opts = [(o, norm(o if isinstance(o, str) else o["text"])) for o in options]
    for o, t in opts:
        if t == a:
            return o
    for o, t in opts:
        if t.startswith(a) or a.startswith(t) and len(t) > 1:
            return o
    for o, t in opts:
        if a and (a in t or t in a) and len(t) > 1:
            return o
    return None


# ---------------------------------------------------------------- resume choice
def choose_resume(text, cfg, base_dir):
    rc = cfg["resumes"]
    t = norm(text)
    best, best_score = rc["default"], 0
    for name, v in rc["variants"].items():
        score = sum(t.count(k.lower()) for k in v.get("keywords", [])) * v.get("weight", 1)
        if score > best_score:
            best, best_score = name, score
    path = Path(base_dir) / cfg["resumes_dir"] / rc["variants"][best]["file"]
    return best, path


# ---------------------------------------------------------------- per-ATS navigation
def application_url(url, ats):
    if ats == "lever" and not url.rstrip("/").endswith("/apply"):
        return url.rstrip("/") + "/apply"
    if ats == "ashby" and not url.rstrip("/").endswith("/application"):
        return url.rstrip("/") + "/application"
    return url


def follow_apply(page):
    """On a listing site (Built In, LinkedIn guest page), click Apply and land on the company's form."""
    btn = page.get_by_role("link", name=re.compile(r"^\s*apply", re.I))
    if btn.count() == 0:
        btn = page.get_by_role("button", name=re.compile(r"^\s*apply", re.I))
    if btn.count() == 0:
        return page
    try:
        with page.context.expect_page(timeout=6000) as new:
            btn.first.click(timeout=4000)
        target = new.value
        target.wait_for_load_state("domcontentloaded", timeout=30000)
        url = target.url
        target.close()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        page.wait_for_timeout(2500)   # same-tab navigation, or nothing happened
    return page


def open_application(page, url, ats):
    page.goto(application_url(url, ats), wait_until="domcontentloaded", timeout=45000)
    if ats in ("builtin", "linkedin") or re.search(r"builtin(boston)?\.com|linkedin\.com", page.url):
        follow_apply(page)
        from .sources import ats_from_url
        new_ats = ats_from_url(page.url)
        if new_ats in ("lever", "ashby"):
            page.goto(application_url(page.url, new_ats), wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    # company career pages often embed the Greenhouse form in an iframe: go to it directly
    for f in page.frames:
        if re.search(r"greenhouse\.io/embed/job_app|boards\.greenhouse\.io/embed", f.url or ""):
            page.goto(f.url, wait_until="domcontentloaded")
            break
    # some pages need an "Apply" click to reveal the form
    if not page.query_selector("input[type=file], input[type=email]"):
        for label in ("Apply for this job", "Apply now", "Apply"):
            btn = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I))
            if btn.count() == 0:
                btn = page.get_by_role("link", name=re.compile(rf"^{label}$", re.I))
            if btn.count():
                try:
                    btn.first.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    break
                except Exception:
                    pass


# ---------------------------------------------------------------- dropdowns
OPTIONS_JS = r"""() => (document.querySelectorAll('[data-jt-opt]').forEach(o => o.removeAttribute('data-jt-opt')),
  [...document.querySelectorAll('[role="option"], [class*="select__option"], [class*="option--"]')]
  .filter(o => { const r = o.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map((o, i) => { o.setAttribute('data-jt-opt', String(i)); return (o.innerText || '').trim(); }))"""

HAS_VALUE_JS = r"""(jt) => { const el = document.querySelector(`[data-jt="${jt}"]`); if (!el) return false;
  const box = el.closest('[class*="control"], [class*="container"]') || el.parentElement;
  return !!(box && box.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value"]')); }"""


def combobox_has_value(page, jt):
    try:
        return page.evaluate(HAS_VALUE_JS, jt)
    except Exception:
        return False


def pick_combobox(page, jt, ans):
    """Open a searchable dropdown, type the answer, click the option that matches best."""
    el = page.locator(f'[data-jt="{jt}"]')
    try:
        el.scroll_into_view_if_needed(timeout=2000)
        el.click(timeout=3000, force=True)
    except Exception:
        return False
    page.wait_for_timeout(250)
    page.keyboard.type(ans[:30], delay=15)     # keyboard works even where .fill() is refused
    page.wait_for_timeout(600)
    texts = page.evaluate(OPTIONS_JS)
    if not texts:                               # not searchable: clear the typing and list everything
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(300)
        texts = page.evaluate(OPTIONS_JS)
    choice = best_option(texts, ans) if texts else None
    if choice is None:
        page.keyboard.press("Escape")
        return False
    try:
        page.locator(f'[data-jt-opt="{texts.index(choice)}"]').first.click(timeout=3000)
    except Exception:
        page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    return True


# ---------------------------------------------------------------- filling
def fill_page(page, rules, resume_path, log):
    filled, missing = 0, []
    fields = page.evaluate(COLLECT_JS)

    # resume upload: the file input that mentions resume/cv, else the first one
    files = [f for f in fields if f["kind"] == "file"]
    target = next((f for f in files if re.search(r"resume|cv", " ".join(
        [f["label"], f["name"], f["id"]]), re.I)), files[0] if files else None)
    if target and resume_path and Path(resume_path).exists():
        try:
            page.set_input_files(f'[data-jt="{target["jt"]}"]', str(resume_path))
            filled += 1
            page.wait_for_timeout(1500)   # some forms parse the resume and prefill fields
            fields = page.evaluate(COLLECT_JS)
        except Exception as e:
            log(f"    resume upload failed: {e}")
    elif target:
        missing.append("Resume (file not found: %s)" % resume_path)

    for f in fields:
        kind, label = f["kind"], f.get("label", "")
        if kind == "file":
            continue
        ans = answer_for(label, rules)
        try:
            if kind in ("text", "textarea"):
                if f.get("value"):
                    continue
                if ans:
                    page.fill(f'[data-jt="{f["jt"]}"]', ans)
                    filled += 1
                elif f.get("required"):
                    missing.append(label)
                    page.evaluate(HIGHLIGHT_JS, f["jt"])
            elif kind == "select":
                choice = best_option(f["options"], ans) if ans else None
                if choice:
                    page.select_option(f'[data-jt="{f["jt"]}"]', label=choice)
                    filled += 1
                elif f.get("required"):
                    missing.append(label)
                    page.evaluate(HIGHLIGHT_JS, f["jt"])
            elif kind == "combobox":
                if f.get("value") or combobox_has_value(page, f["jt"]):
                    continue
                if ans and pick_combobox(page, f["jt"], ans):
                    filled += 1
                elif ans:
                    missing.append(f"{label} (no option matched '{ans}')")
                    page.evaluate(HIGHLIGHT_JS, f["jt"])
                elif f.get("required"):
                    missing.append(label)
                    page.evaluate(HIGHLIGHT_JS, f["jt"])
            elif kind in ("radio", "checkbox", "buttons"):
                if f.get("checked"):
                    continue
                choice = best_option(f["options"], ans) if ans else None
                if choice:
                    page.click(f'[data-jt="{choice["jt"]}"]')
                    filled += 1
                elif f.get("required") or kind == "buttons":
                    missing.append(label or "(unlabeled choice)")
                    for o in f["options"]:
                        page.evaluate(HIGHLIGHT_JS, o["jt"])
        except Exception as e:
            missing.append(f"{label} (could not fill: {type(e).__name__})")
    out = []
    for m in missing:
        if m and m not in out:
            out.append(m)
    return filled, out


def page_title(page):
    """Best guess at the job title for links pasted into urls.txt."""
    try:
        h1 = page.locator("h1").first
        t = h1.inner_text(timeout=2000).strip() if h1.count() else ""
    except Exception:
        t = ""
    if not t or len(t) > 120:
        t = re.split(r"\s[-|@]\s", page.title() or "")[0].strip()
    return t[:120] or None


# ---------------------------------------------------------------- main entry
CLOSED_RE = re.compile(r"(no longer|not currently) (accepting|available|open)|position (has been )?filled|job not found|"
                       r"this job (is|has) (closed|expired)|page you('re| are) looking for", re.I)


def prepare(ctx, db, cfg, rules, base_dir, job, log):
    """Open one job in a new tab and prefill it. Returns the page, or None if there's nothing to review."""
    log(f"\n[{job['id']}] {job['company']} - {job['title']}")
    page = ctx.new_page()
    page.set_default_timeout(5000)            # never hang 30s on one field while you're typing
    try:
        if "linkedin.com" in (job["url"] or ""):
            try:
                page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
                save_recruiter(db, dict(job), page)
            except Exception:
                pass
        open_application(page, job["url"], job["ats"])
        text = page.inner_text("body")[:20000]
        if CLOSED_RE.search(text[:4000]):
            log("    posting is closed")
            db.set(job["id"], status="closed")
            page.close()
            return None
        if job["title"] == "(from urls.txt)":
            real = page_title(page)
            if real:
                db.set(job["id"], title=real)
        variant, resume = choose_resume(text, cfg, base_dir)
        filled, missing = fill_page(page, rules, resume, log)
        if (re.search(r"create (an )?account|sign in|log in", text[:6000], re.I) and filled < 3) or \
                "linkedin.com" in page.url:
            status = "needs_you"
            missing.insert(0, "Site wants an account or sign-in (or it's LinkedIn Easy Apply)")
        else:
            status = "filled"
        note = ("Needs: " + "; ".join(missing[:12])) if missing else "All fields filled"
        db.set(job["id"], status=status, resume=variant, notes=note)
        log(f"    resume: {variant} | filled {filled} fields | {note}")
        log(f"    READY - tab is yours: [{job['id']}] {job['company']}")
        return page
    except Exception as e:
        log(f"    could not open or fill: {type(e).__name__}: {str(e)[:120]}")
        db.set(job["id"], status="needs_you", notes=f"Open manually: {type(e).__name__}")
        return page if not page.is_closed() else None


def run(db, cfg, profile, base_dir, jobs, headless=False, log=print, watch=True, keep_open=None):
    jobs = [dict(j) for j in jobs]
    """Fill jobs. keep_open=N: keep N prefilled tabs open, and open the next job each time you
    submit or close one, until the queue runs out (good for long sessions)."""
    from playwright.sync_api import sync_playwright

    rules = build_rules(profile)
    ac = cfg.get("apply", {})
    queue = list(jobs)
    with sync_playwright() as pw:
        kwargs = dict(headless=headless, slow_mo=ac.get("slow_mo_ms", 0), **window_opts(headless))
        if ac.get("browser", "chrome") == "chrome":
            kwargs["channel"] = "chrome"
        ctx = pw.chromium.launch_persistent_context(str(Path(base_dir) / "data" / "browser-profile"), **kwargs)
        open_pages = {}
        target = keep_open or len(queue)

        def top_up():
            while queue and len(open_pages) < target:
                job = queue.pop(0)
                pg = prepare(ctx, db, cfg, rules, base_dir, job, log)
                if pg:
                    open_pages[job["id"]] = pg
            if open_pages:
                try:
                    next(iter(open_pages.values())).bring_to_front()
                except Exception:
                    pass

        top_up()
        if not watch or headless:
            ctx.close()
            return
        log("\nReview each tab, fix anything outlined in red, and click Submit yourself.")
        log("I mark each one submitted when its confirmation page appears." +
            (f" Close a tab to skip it; I'll keep {target} open until the queue ({len(queue)} left) is empty."
             if keep_open else ""))
        log("Close the browser (or press Ctrl+C) when you're done.\n")
        try:
            while open_pages:
                changed = False
                for jid, pg in list(open_pages.items()):
                    if pg.is_closed():
                        db.set(jid, status="skipped")
                        log(f"[{jid}] skipped (tab closed)")
                        del open_pages[jid]
                        changed = True
                        continue
                    try:
                        body = pg.evaluate("() => (document.body && document.body.innerText || '').slice(0, 5000)")
                    except Exception:
                        continue
                    if CONFIRM_RE.search(body) or re.search(r"confirmation|thank", pg.url, re.I):
                        db.set(jid, status="submitted")
                        log(f"[{jid}] submitted" + (f"  ({len(queue)} left in queue)" if keep_open else ""))
                        del open_pages[jid]
                        changed = True
                        if keep_open:
                            pg.wait_for_timeout(1500)
                            pg.close()        # keep the window tidy in long sessions
                if changed and keep_open:
                    top_up()
                time.sleep(3)
        except KeyboardInterrupt:
            pass
        except Exception as e:   # browser closed is normal; anything else gets reported
            if "closed" not in str(e).lower():
                log(f"Stopped because of an error: {type(e).__name__}: {str(e)[:200]}")
        try:
            ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------- LinkedIn Easy Apply sessions
# The tool only opens each job page. You click Easy Apply and submit yourself (LinkedIn prefills
# most of it from your profile). It never clicks anything on LinkedIn for you.
JOB_CLOSED_RE = re.compile(r"(no longer|not currently) accepting applications|applications? (are|is) (now )?closed|"
                           r"this job (is|has) (no longer available|closed|expired)|job (has been )?(closed|filled)", re.I)
# Wellfound (and other non-LinkedIn sites): only an explicit confirmation counts. Their pages have an
# "Applied" link in the navigation, so a bare "Applied" proves nothing there.
OTHER_DONE_RE = re.compile(r"application (was |has been )?(sent|submitted)|you('ve| have)? applied|successfully applied|"
                           r"scheduled to (be )?submit|submits? in \d+ min|\bundo\b\W{0,4}\d{1,2}:\d\d|"
                           r"thanks? (you )?for applying", re.I)
EASY_DONE_RE = re.compile(r"your application was sent|application submitted|application sent|^applied$|\bapplied \d+ (second|minute|hour|day|week|month)s? ago|"
                          r"\bapplied (just now|today)|you applied", re.I | re.M)
LOGIN_WALL_RE = re.compile(r"sign in to (apply|see|view)|join (now )?to (apply|see)|agree & join", re.I)

HIRING_TEAM_JS = r"""() => {
  const out = [];
  const heads = [...document.querySelectorAll('h2, h3, span, div')]
    .filter(e => /meet the hiring team|people you can reach out to|job poster/i.test(e.innerText || '') && (e.innerText || '').length < 60);
  for (const h of heads) {
    let box = h; for (let i = 0; i < 5 && box.parentElement; i++) box = box.parentElement;
    box.querySelectorAll('a[href*="/in/"]').forEach(a => {
      const url = a.href.split('?')[0];
      const name = (a.innerText || a.getAttribute('aria-label') || '').trim().split('\n')[0];
      if (!out.some(o => o.url === url)) out.push({name, url});
    });
    if (out.length) break;
  }
  return out.slice(0, 3);
}"""


# Only look at this job's own header/apply area and any popup, never the whole page:
# the "similar jobs" sidebar shows "Applied" badges for OTHER jobs you applied to.
APPLY_STATE_JS = r"""() => {
  const parts = [];
  document.querySelectorAll('[role="dialog"], [class*="artdeco-modal"]').forEach(d => parts.push(d.innerText || ''));
  const sel = ['[class*="unified-top-card"]', '[class*="top-card"]', '[class*="jobs-s-apply"]',
               '[class*="apply-button"]', '[class*="post-apply"]', '[class*="feedback-message"]'];
  const main = document.querySelector('main') || document.body;
  sel.forEach(s => main.querySelectorAll(s).forEach(e => {
    if (!e.closest('[class*="similar"], [class*="recommend"], aside, [class*="jobs-search-results"]'))
      parts.push(e.innerText || '');
  }));
  if (!parts.join('').trim()) parts.push((document.body.innerText || '').slice(0, 1200));  // fallback: page top only
  return parts.join('\n').slice(0, 6000);
}"""


DESC_JS = r"""() => { const d = document.querySelector('[class*="jobs-description"], #job-details, [class*="description__text"], [class*="show-more-less-html"]');
  return (d ? d.innerText : '').slice(0, 30000); }"""


def description_text(pg):
    try:
        return pg.evaluate(DESC_JS)
    except Exception:
        return ""


# What kind of apply button does this LinkedIn job have? (signed in, the real button is visible)
APPLY_KIND_JS = r"""() => {
  const vis = b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const inCard = b => b.closest('aside, li, [class*="job-card"], [class*="similar"], [class*="recommend"], [class*="jobs-search-results"], [class*="discovery"]');
  const lab = b => ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
  // 1. LinkedIn's own apply button in the job's header
  const primary = [...document.querySelectorAll('.jobs-apply-button, [id^="jobs-apply-button"], [class*="jobs-apply-button"] button, button[aria-label*="Apply" i], a[aria-label*="Apply" i]')]
    .filter(b => vis(b) && !inCard(b));
  if (primary.length) return /easy apply/i.test(lab(primary[0])) ? 'easy' : 'external';
  const main = document.querySelector('main') || document.body;
  const btns = [...main.querySelectorAll('button, a')].filter(b => {
    if (inCard(b)) return false;
    const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0;
  });
  const label = b => ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).trim();
  if (btns.some(b => /easy apply/i.test(label(b)))) return 'easy';
  if (btns.some(b => /company website|apply on|^\s*apply\s*$/i.test(label(b)) || /^\s*apply\b/i.test(b.innerText || ''))) return 'external';
  return 'unknown';
}"""


def apply_kind(pg):
    try:
        return pg.evaluate(APPLY_KIND_JS)
    except Exception:
        return "unknown"


# The job's OWN button: 'Undo · 4:12' during Wellfound's hold, then 'Applied'. Ignores menus, and pop-up /
# pinned notices (Wellfound shows another job's pending-application notice on every page).
APPLIED_BUTTON_JS = r"""() => {
  const pinned = el => { for (let e = el; e && e !== document.body; e = e.parentElement) {
      const p = getComputedStyle(e).position; if (p === 'fixed') return true; } return false; };
  return [...document.querySelectorAll('button, [role="button"]')].some(b => {
    const t = (b.innerText || '').replace(/\s+/g, ' ').trim();
    if (!/^(applied|undo\W{0,4}\d{1,2}:\d\d)$/i.test(t)) return false;
    if (b.closest('nav, header, aside, [role="navigation"], [role="status"], [role="alert"], [class*="toast"], [class*="snackbar"], [class*="banner"]')) return false;
    return b.getBoundingClientRect().width > 0 && !pinned(b);
  });
}"""


def applied_button(pg):
    """The job's own Apply button now reads 'Applied' (Wellfound after the Undo timer). Menu links don't count."""
    try:
        return pg.evaluate(APPLIED_BUTTON_JS)
    except Exception:
        return False


def dialog_and_top(pg):
    """Popups plus the main content (navigation/header excluded) for non-LinkedIn sites."""
    try:
        return pg.evaluate(r"""() => {
          const parts = [...document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="toast"]')].map(d => d.innerText || '');
          const main = document.querySelector('main') || document.body;
          const clone = main.cloneNode(true);
          clone.querySelectorAll('nav, header, [role="navigation"]').forEach(n => n.remove());
          parts.push((clone.innerText || '').slice(0, 4000));
          return parts.join('\n');
        }""")
    except Exception:
        return ""


def apply_state(pg):
    try:
        return pg.evaluate(APPLY_STATE_JS)
    except Exception:
        return ""


def linkedin_signed_in(ctx):
    try:
        return any(c["name"] == "li_at" for c in ctx.cookies("https://www.linkedin.com"))
    except Exception:
        return False


def run_easy(db, cfg, base_dir, jobs, keep_open=5, log=print, headless=False, need_login=True):
    from playwright.sync_api import sync_playwright
    ac = cfg.get("apply", {})
    queue = [dict(j) for j in jobs]
    with sync_playwright() as pw:
        kwargs = dict(headless=headless, **window_opts(headless))
        if ac.get("browser", "chrome") == "chrome":
            kwargs["channel"] = "chrome"
        ctx = pw.chromium.launch_persistent_context(str(Path(base_dir) / "data" / "browser-profile"), **kwargs)
        open_pages, warned = {}, True

        if need_login and any("linkedin.com" in (j["url"] or "") for j in queue) and not linkedin_signed_in(ctx):
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            log("Sign in to LinkedIn in the browser window (the tool remembers it after this).")
            log("I'll start opening jobs as soon as you're signed in...")
            try:
                while not linkedin_signed_in(ctx):
                    if not ctx.pages:
                        log("Browser closed before sign-in.")
                        return
                    time.sleep(2)
            except KeyboardInterrupt:
                return
            log("Signed in.\n")
            try:
                pg.close()
            except Exception:
                pass

        def open_next():
            nonlocal warned
            while queue and len(open_pages) < keep_open:
                job = queue.pop(0)
                pg = ctx.new_page()
                pg.set_default_timeout(8000)
                try:
                    pg.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
                    pg.wait_for_timeout(2500)
                    body = pg.evaluate("() => (document.body && document.body.innerText || '').slice(0, 8000)")
                except Exception as e:
                    log(f"[{job['id']}] could not open: {type(e).__name__}")
                    pg.close()
                    continue
                is_li = "linkedin.com" in (job["url"] or "")
                if (is_li and EASY_DONE_RE.search(apply_state(pg))) or (not is_li and applied_button(pg)):
                    db.set(job["id"], status="submitted", notes="Already applied" + (" on LinkedIn" if is_li else ""))
                    log(f"[{job['id']}] already applied - skipped")
                    pg.close()
                    continue
                if "linkedin.com" in (job["url"] or "") and wrong_place(cfg, job, pg):
                    skip_wrong_place(db, job, pg, log)
                    continue
                kind = apply_kind(pg) if "linkedin.com" in (job["url"] or "") else "easy"
                if kind == "external":
                    move_to_forms(db, job, pg, log)
                    continue
                job["_kind"], job["_opened"] = kind, time.time()
                if kind == "easy" and job.get("ats") == "linkedin_check":
                    db.set(job["id"], ats="linkedin_easy")
                if JOB_CLOSED_RE.search(apply_state(pg) + "\n" + body[:3000]):
                    db.set(job["id"], status="closed")
                    log(f"[{job['id']}] closed - skipped")
                    pg.close()
                    continue
                if LOGIN_WALL_RE.search(body) and not warned:
                    warned = True
                    log("\n  ** Sign in to LinkedIn in this window once (the tool remembers it), then reload the tab. **\n")
                desc = description_text(pg)
                from .sources import remote_conflict, in_target_area
                from .boards import Filters
                flt = Filters(cfg)
                if not in_target_area(flt, job["location"]) and remote_conflict(desc, flt):
                    db.set(job["id"], status="skipped", notes="Listed as remote, but on-site/hybrid/relocate outside your area")
                    log(f"[{job['id']}] {job['company']} - not actually remote, skipped")
                    pg.close()
                    continue
                save_recruiter(db, job, pg)
                open_pages[job["id"]] = (job, pg)
                try:   # same keyword scoring the form filler uses, run on the full job description
                    variant, _ = choose_resume(job["title"] + " " + (desc or ""), cfg, base_dir)
                    db.set(job["id"], resume=variant)
                except Exception:
                    variant = cfg.get("resumes", {}).get("default", "?")
                log(f"[{job['id']}] {job['company']} - {job['title']}   ->  resume: {variant.upper()}")

        open_next()
        log(f"\nClick Easy Apply in each tab and submit. I mark each one when LinkedIn confirms it, and open the next."
            f" Close a tab to SKIP that job (it won't come back). {len(queue)} more in the queue. Ctrl+C or close the browser to stop.\n")
        try:
            while open_pages:
                changed = False
                for jid, (job, pg) in list(open_pages.items()):
                    if pg.is_closed():
                        db.set(jid, status="skipped")
                        log(f"[{jid}] skipped (tab closed)")
                        del open_pages[jid]
                        changed = True
                        continue
                    if not job.get("_rec"):
                        save_recruiter(db, job, pg)
                    if time.time() - job.get("_opened", 0) < 30 and JOB_CLOSED_RE.search(apply_state(pg)):
                        db.set(jid, status="closed")
                        log(f"[{jid}] {job['company']} - not accepting applications, marked closed")
                        del open_pages[jid]
                        changed = True
                        pg.close()
                        continue
                    if time.time() - job.get("_opened", 0) < 30 and not job.get("_placed") \
                            and "linkedin.com" in (job["url"] or "") and apply_state(pg):
                        if wrong_place(cfg, job, pg):
                            del open_pages[jid]
                            skip_wrong_place(db, job, pg, log)
                            changed = True
                            continue
                        job["_placed"] = re.search(r"on-?site|hybrid|remote", apply_state(pg), re.I) is not None
                    if job.get("_kind") == "unknown" and time.time() - job["_opened"] < 30:
                        job["_kind"] = apply_kind(pg)
                        if job["_kind"] == "external":
                            del open_pages[jid]
                            move_to_forms(db, job, pg, log)
                            changed = True
                            continue
                    if "linkedin.com" in (job["url"] or ""):
                        done = EASY_DONE_RE.search(apply_state(pg))
                    else:
                        done = applied_button(pg) if "wellfound.com" in (pg.url or "") else \
                            (OTHER_DONE_RE.search(dialog_and_top(pg)) or applied_button(pg))
                    if done:
                        db.set(jid, status="submitted")
                        del open_pages[jid]
                        changed = True
                        if "linkedin.com" in (job["url"] or ""):
                            log(f"[{jid}] submitted  ({len(queue)} left in queue)")
                            pg.wait_for_timeout(1500)
                            pg.close()
                        else:   # Wellfound holds applications ~5 min with an Undo button: leave the tab alone
                            log(f"[{jid}] submitted - tab left open so you can still Undo  ({len(queue)} left in queue)")
                if changed:
                    open_next()
                time.sleep(3)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            if "closed" not in str(e).lower():
                log(f"Stopped because of an error: {type(e).__name__}: {str(e)[:200]}")
        try:
            ctx.close()
        except Exception:
            pass


def wrong_place(cfg, job, pg):
    """Signed-in LinkedIn shows a workplace label (On-site / Hybrid / Remote) in the job header.
    True if the job is on-site or hybrid somewhere outside your configured area."""
    from .sources import in_target_area
    from .boards import Filters
    head = apply_state(pg)
    if not head:
        return False
    on_site = re.search(r"\b(on-?site|hybrid)", head, re.I)   # labels can run together: "On-siteFull-time"
    remote = re.search(r"\bremote", head, re.I)
    if not on_site or remote:
        return False
    first = head.split("\n")[:12]                     # header lines include the job's location
    return not in_target_area(Filters(cfg), " ".join(first) + " " + (job.get("location") or "").replace("(Remote)", ""))


def header_place(pg):
    """'On-site in Morrisville, NC' from the LinkedIn job header, for logging."""
    head = apply_state(pg)
    kind = re.search(r"\b(on-?site|hybrid|remote)", head, re.I)
    loc = next((l.split("·")[0].strip() for l in head.split("\n") if "·" in l), "")
    return f"{kind.group(1).capitalize() if kind else '?'} in {loc or 'unknown location'}"


def skip_wrong_place(db, job, pg, log):
    where = header_place(pg)
    db.set(job["id"], status="skipped", notes=f"{where} - outside your area (LinkedIn's workplace label)")
    log(f"[{job['id']}] {job['company']} - {where}, skipped")
    try:
        pg.close()
    except Exception:
        pass


def move_to_forms(db, job, pg, log):
    db.set(job["id"], ats="linkedin")   # not Easy Apply: apply --marathon follows it to the company form
    log(f"[{job['id']}] {job['company']} - applies on the company's site, moved to the forms queue (`apply --marathon`)")
    try:
        pg.close()
    except Exception:
        pass


def save_recruiter(db, job, pg):
    """If the job page shows a hiring team / job poster, save their LinkedIn profile links."""
    try:
        people = pg.evaluate(HIRING_TEAM_JS)
    except Exception:
        people = []
    if people:
        job["_rec"] = True
        db.set(job["id"], recruiter="; ".join(f"{p['name']} {p['url']}".strip() for p in people))
