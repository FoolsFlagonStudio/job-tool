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


def open_application(page, url, ats):
    page.goto(application_url(url, ats), wait_until="domcontentloaded", timeout=45000)
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
                if f.get("value"):
                    continue
                if ans:
                    el = page.locator(f'[data-jt="{f["jt"]}"]')
                    el.click()
                    el.fill(ans)
                    page.wait_for_timeout(700)
                    opt = page.get_by_role("option").filter(has_text=re.compile(re.escape(ans[:20]), re.I))
                    if opt.count():
                        opt.first.click()
                    else:
                        el.press("Enter")
                    filled += 1
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
    return filled, [m for m in missing if m]


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
def run(db, cfg, profile, base_dir, jobs, headless=False, log=print, watch=True):
    from playwright.sync_api import sync_playwright

    rules = build_rules(profile)
    ac = cfg.get("apply", {})
    with sync_playwright() as pw:
        kwargs = dict(headless=headless, slow_mo=ac.get("slow_mo_ms", 50),
                      viewport={"width": 1280, "height": 900})
        if ac.get("browser", "chrome") == "chrome":
            kwargs["channel"] = "chrome"
        ctx = pw.chromium.launch_persistent_context(str(Path(base_dir) / "data" / "browser-profile"), **kwargs)
        open_pages = {}
        for job in jobs:
            log(f"\n[{job['id']}] {job['company']} - {job['title']}")
            page = ctx.new_page()
            try:
                open_application(page, job["url"], job["ats"])
                text = page.inner_text("body")[:20000]
                if re.search(r"no longer (accepting|available|open)|position (has been )?filled|job not found", text, re.I):
                    log("    posting is closed")
                    db.set(job["id"], status="closed")
                    page.close()
                    continue
                if job["title"] == "(from urls.txt)":
                    real = page_title(page)
                    if real:
                        db.set(job["id"], title=real)
                variant, resume = choose_resume(text, cfg, base_dir)
                filled, missing = fill_page(page, rules, resume, log)
                if re.search(r"create (an )?account|sign in to apply|log in to apply", text, re.I) and filled < 3:
                    status = "needs_you"
                    missing.insert(0, "Site wants an account or sign-in")
                else:
                    status = "filled"
                note = ("Needs: " + "; ".join(missing[:12])) if missing else "All fields filled"
                db.set(job["id"], status=status, resume=variant, notes=note)
                log(f"    resume: {variant} | filled {filled} fields | {note}")
                open_pages[job["id"]] = page
            except Exception as e:
                log(f"    could not open or fill: {type(e).__name__}: {str(e)[:120]}")
                db.set(job["id"], status="needs_you", notes=f"Open manually: {type(e).__name__}")
        if not watch or headless:
            ctx.close()
            return
        log("\nReview each tab, fix anything highlighted in red, and click Submit yourself.")
        log("I'll mark each one submitted when its confirmation page appears.")
        log("Close the browser (or press Ctrl+C) when you're done.\n")
        try:
            while open_pages:
                for jid, pg in list(open_pages.items()):
                    if pg.is_closed():
                        del open_pages[jid]
                        continue
                    try:
                        body = pg.inner_text("body", timeout=2000)[:5000]
                    except Exception:
                        continue
                    if CONFIRM_RE.search(body) or re.search(r"confirmation|thank", pg.url, re.I):
                        db.set(jid, status="submitted")
                        log(f"[{jid}] submitted")
                        del open_pages[jid]
                time.sleep(3)
        except KeyboardInterrupt:
            pass
        except Exception:
            pass   # browser closed
        try:
            ctx.close()
        except Exception:
            pass
