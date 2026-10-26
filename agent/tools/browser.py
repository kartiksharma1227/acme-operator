"""Browser tools: operate web systems the way a person does, through a real browser.

The model sees pages as accessibility snapshots (roles + names), not raw HTML or pixels:
compact, stable across styling changes, and the same names it then uses to click.
After every action the tool classifies what happened, so the model gets a typed result:
  transient  - server error / timeout (already retried here with backoff)
  auth       - we were signed in and got bounced to a sign-in form
  validation - a form submission was rejected; the page's error text is included
  not_found / ambiguous - no / several elements match; the alternatives are listed
"""
from __future__ import annotations

import atexit
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Browser, BrowserContext, Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from agent.models import ToolResult
from agent.recovery import note_recovery
from agent.tools.base import Tool
from agent.tools.credentials import credentials_for, resolve_secret

if TYPE_CHECKING:
    from agent.tools.base import RunContext

SNAPSHOT_LIMIT = 6000
RETRY_DELAYS = (0.5, 1.0, 2.0)
NAV_TIMEOUT_MS = 20_000
ROLES = ["button", "link", "checkbox", "radio", "tab", "menuitem", "option", "textbox", "combobox"]

# A click "submits" when it targets a submit control that belongs to a form.
IS_SUBMIT_JS = "el => !!el.form && el.type === 'submit'"


# Playwright's sync API allows one driver per thread, so the process shares one driver and one
# browser per headless setting. Each session gets its own browser *context*: separate cookies,
# storage and sign-in, so the verifier's session is as independent as a different browser.
_driver = None
_browsers: dict[bool, Browser] = {}


def _shared_browser(headless: bool) -> Browser:
    global _driver
    if _driver is None:
        _driver = sync_playwright().start()
        atexit.register(_shutdown)
    browser = _browsers.get(headless)
    if browser is None or not browser.is_connected():
        browser = _browsers[headless] = _driver.chromium.launch(headless=headless)
    return browser


def _shutdown() -> None:
    global _driver
    try:
        for browser in _browsers.values():
            browser.close()
        if _driver is not None:
            _driver.stop()
    except Exception:
        pass
    _browsers.clear()
    _driver = None


class BrowserSession:
    """One isolated browser context and page per session. Opened lazily on first use."""

    def __init__(self, ctx: "RunContext"):
        self.ctx = ctx
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.last_status: int | None = None  # HTTP status of the latest main-frame document
        self.nav_log: list[dict] = []        # main-frame responses since the last click
        self.logged_in = False

    def ensure(self) -> Page:
        if self.page is None:
            self._context = _shared_browser(self.ctx.config.headless).new_context(viewport={"width": 1280, "height": 900})
            self._context.set_default_timeout(15_000)
            self.page = self._context.new_page()
            self.page.on("response", self._on_response)
        return self.page

    def _on_response(self, response) -> None:
        request = response.request
        if request.is_navigation_request() and self.page and request.frame == self.page.main_frame:
            self.last_status = response.status
            self.nav_log.append({"method": request.method, "status": response.status, "url": response.url,
                                 "location": response.headers.get("location")})

    def accepted_submission(self) -> str | None:
        """Where the server sent us after accepting the last form post, if it did.

        Post/redirect/get: a server that accepts a form answers the POST with a redirect to the
        result. If that redirect went anywhere other than where we ended up on a sign-in form,
        the submission was saved even if the page after it then failed or asked us to sign in."""
        post = next((r for r in self.nav_log if r["method"] == "POST"), None)
        if not post or not (300 <= post["status"] < 400) or not post["location"]:
            return None
        target = urlparse(urljoin(post["url"], post["location"]))
        landed = urlparse(self.page.url) if self.page else None
        if landed and on_login_form(self.page) and target.path == landed.path:
            return None  # the POST itself was bounced to sign-in: nothing was saved
        return target.path + (f"?{target.query}" if target.query else "")

    def screenshot(self, note: str) -> str:
        folder = self.ctx.run_dir / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        # Numbered by what's on disk, so several browsers in one run (operator, verifier, a resumed
        # process) never overwrite each other's evidence.
        number = len(list(folder.glob("*.png"))) + 1
        slug = re.sub(r"[^a-z0-9]+", "-", note.lower()).strip("-")[:40] or "page"
        path = folder / f"{number:03d}-{slug}.png"
        self.ensure().screenshot(path=str(path), full_page=True)
        return str(path.relative_to(self.ctx.run_dir))

    def close(self) -> None:
        # Best effort: closing must never mask the run's real outcome.
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        self.page = self._context = None


# ---------- observing and classifying ----------

def on_login_form(page: Page) -> bool:
    return page.locator("input[type=password]").count() > 0


def _texts(page: Page, role: str) -> list[str]:
    return [t.strip() for t in page.get_by_role(role).all_inner_texts() if t.strip()]


def observe(session: BrowserSession) -> dict:
    page = session.ensure()
    root = page.locator("main") if page.locator("main").count() else page.locator("body")
    try:
        snapshot = root.aria_snapshot()
    except PlaywrightError:
        snapshot = root.inner_text()
    if len(snapshot) > SNAPSHOT_LIMIT:
        snapshot = snapshot[:SNAPSHOT_LIMIT] + "\n… (truncated; navigate or search to narrow down)"
    return {
        "url": page.url,
        "title": page.title(),
        "status": session.last_status,
        "alerts": _texts(page, "alert"),
        "notices": _texts(page, "status"),
        "page": snapshot,
    }


def _visible_controls(page: Page) -> dict:
    return {
        "buttons": _texts(page, "button")[:30],
        "links": _texts(page, "link")[:40],
        "fields": [t.strip() for t in page.locator("label").all_inner_texts() if t.strip()][:30],
    }


def _settle_server_errors(session: BrowserSession) -> ToolResult | None:
    """Server errors on a GET are transient: reload with backoff before bothering the model."""
    page = session.ensure()
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if not (session.last_status and session.last_status >= 500):
            return None
        note_recovery(session.ctx, "transient_retry", attempt=attempt, status=session.last_status, url=page.url)
        time.sleep(delay)
        try:
            page.reload(wait_until="load", timeout=NAV_TIMEOUT_MS)
        except PlaywrightTimeout:
            continue
    if session.last_status and session.last_status >= 500:
        return ToolResult.fail("transient", f"The server kept returning HTTP {session.last_status} after "
                                            f"{len(RETRY_DELAYS)} retries. Wait and try again, or continue with other work.",
                               data=observe(session))
    return None


def _session_expired(session: BrowserSession) -> ToolResult:
    session.logged_in = False
    note_recovery(session.ctx, "session_expired", url=session.ensure().url)
    return ToolResult.fail("auth", "Session expired or not signed in: the page is now a sign-in form. "
                                   "Sign in again (browser_sign_in), then continue where you left off. "
                                   "Any form you were filling was NOT submitted.",
                           data=observe(session))


def _accepted_but_unconfirmed(failure: ToolResult, location: str, what_happened: str) -> ToolResult:
    """The server accepted the submission, but we couldn't see the confirmation. Say so plainly:
    telling the model "nothing was saved" here would invite a duplicate."""
    data = failure.data if isinstance(failure.data, dict) else {}
    failure.data = {**data, "submission_accepted": location}
    failure.message = (f"Your submission WAS accepted (the system redirected to {location}), but {what_happened}. "
                       f"Do NOT submit it again. Sign in again if needed and open {location} to confirm the result.")
    return failure


def _require_page(ctx: "RunContext") -> ToolResult | None:
    if ctx.browser.page is None or ctx.browser.page.url in ("", "about:blank"):
        return ToolResult.fail("validation", "No page is open yet. Use browser_navigate first.")
    return None


def _locate(page: Page, role: str, name: str) -> tuple[Locator | None, ToolResult | None]:
    exact = page.get_by_role(role, name=name, exact=True)
    matches = exact if exact.count() else page.get_by_role(role, name=name)
    count = matches.count()
    if count == 1:
        return matches.first, None
    if count == 0:
        return None, ToolResult.fail(
            "not_found",
            f"No {role} named '{name}' on this page. The UI may have changed: pick the control with the same "
            f"meaning from the list below (labels can differ), or navigate elsewhere.",
            data=_visible_controls(page))
    # Several matches pointing at the same place (e.g. a menu link and a breadcrumb) are equivalent.
    hrefs = {matches.nth(i).get_attribute("href") for i in range(count)}
    if role == "link" and len(hrefs) == 1 and None not in hrefs:
        return matches.first, None
    candidates = [matches.nth(i).inner_text().strip() for i in range(min(count, 10))]
    return None, ToolResult.fail("ambiguous", f"{count} elements match {role} '{name}'. Use a more specific name.",
                                 data={"candidates": candidates})


# ---------- tools ----------

class BrowserNavigate(Tool):
    name = "browser_navigate"
    description = ("Open a URL in the browser. Relative paths (e.g. '/some/page') are resolved against the "
                   "web system's base URL. Returns an accessibility snapshot of the page, its HTTP status, "
                   "and any alert/notice messages. A 404 status is information, not a failure.")
    changes_view = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL or a path relative to the system's base URL"},
            "system": {"type": "string", "description": "Which web system (only needed if there are several)"},
        },
        "required": ["url"],
    }

    def run(self, ctx, url: str, system: str | None = None) -> ToolResult:
        if not url.startswith(("http://", "https://")):
            system = system or ctx.config.default_web_system()
            if system is None:
                return ToolResult.fail("validation", f"Relative URL needs a system. Web systems: {list(ctx.config.web_systems())}")
            url = urljoin(ctx.config.base_url(system) + "/", url.lstrip("/"))

        session = ctx.browser
        page = session.ensure()
        last_error = ""
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
                break
            except (PlaywrightTimeout, PlaywrightError) as exc:
                last_error = str(exc).splitlines()[0]
                if attempt == len(RETRY_DELAYS):
                    return ToolResult.fail("transient", f"Could not load {url}: {last_error}. The system may be down; "
                                                        "try again later or continue with other work.")
                note_recovery(ctx, "transient_retry", attempt=attempt + 1, url=url, error=last_error)
                time.sleep(RETRY_DELAYS[attempt])

        if failure := _settle_server_errors(session):
            return failure
        if on_login_form(page):
            if session.logged_in:
                return _session_expired(session)
            return ToolResult.success(observe(session), message="This page requires signing in. Use browser_sign_in.")
        return ToolResult.success(observe(session))


class BrowserSnapshot(Tool):
    name = "browser_snapshot"
    description = "Return the current page's accessibility snapshot, URL, HTTP status and alert/notice messages."

    def run(self, ctx) -> ToolResult:
        if failure := _require_page(ctx):
            return failure
        return ToolResult.success(observe(ctx.browser))


class BrowserClick(Tool):
    name = "browser_click"
    description = ("Click an element identified by its ARIA role and accessible name, exactly as shown in the "
                   "snapshot (e.g. role='button', name='Save'). Clicking a form's submit button submits the "
                   "form; meaningful submissions are checked against company policy first and may need human "
                   "approval. Returns the resulting page.")
    parameters = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": ROLES},
            "name": {"type": "string", "description": "Accessible name of the element"},
        },
        "required": ["role", "name"],
    }
    risk = "write"
    read_only = False
    changes_view = True

    def run(self, ctx, role: str, name: str) -> ToolResult:
        if failure := _require_page(ctx):
            return failure
        session = ctx.browser
        page = session.ensure()
        target, failure = _locate(page, role, name)
        if failure:
            return failure

        is_submit = bool(target.evaluate(IS_SUBMIT_JS))
        if is_submit and ctx.gate is not None:
            if blocked := ctx.gate(ctx, page, target):
                return blocked

        result = self._perform(session, page, target, role, name, is_submit)
        if is_submit and hasattr(ctx.gate, "record"):
            ctx.gate.record(ctx, result)  # the gate only remembers submissions that actually succeeded
        return result

    @staticmethod
    def _perform(session: BrowserSession, page: Page, target: Locator, role: str, name: str,
                 is_submit: bool) -> ToolResult:
        """Click and classify what happened. `is_submit` marks a data-changing submission (the
        gate's business); a read-only search form still navigates but isn't one."""
        was_login_form = on_login_form(page)
        href = target.get_attribute("href") if role == "link" else None
        navigates = bool(target.evaluate(IS_SUBMIT_JS)) or (href is not None and not href.startswith("#"))
        session.nav_log.clear()
        try:
            if navigates:
                with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
                    target.click()
            else:
                target.click()
                page.wait_for_load_state("load")
        except PlaywrightTimeout:
            timed_out = ToolResult.fail("transient", f"Clicking {role} '{name}' timed out waiting for the page. "
                                                     "Take a snapshot to see where things stand before retrying "
                                                     "(a submitted form may already have been saved).",
                                        data=observe(session))
            if is_submit and (accepted := session.accepted_submission()):
                return _accepted_but_unconfirmed(timed_out, accepted, "the result page did not finish loading")
            return timed_out

        accepted = session.accepted_submission() if is_submit else None
        if failure := _settle_server_errors(session):
            if accepted:
                return _accepted_but_unconfirmed(failure, accepted, "the result page kept failing to load")
            if is_submit:  # the error came after the form went out, so it may well have been saved
                failure.message += (" The form was sent before this error, so it may already have been saved: "
                                    "check for the record before submitting again.")
            return failure
        shot = session.screenshot(f"after-click-{name}")

        if on_login_form(page):
            if was_login_form:
                return ToolResult.fail("validation", "Sign-in failed.", data=observe(session), evidence=[shot])
            if session.logged_in:
                expired = _session_expired(session)
                if accepted:
                    return _accepted_but_unconfirmed(expired, accepted, "the session expired right after")
                return expired
        elif was_login_form:
            session.logged_in = True

        obs = observe(session)
        if is_submit and session.last_status and 400 <= session.last_status < 500 and obs["alerts"]:
            return ToolResult.fail("validation", "The form was rejected and nothing was saved: "
                                                 + " ".join(obs["alerts"]) + " Fix the fields and submit again.",
                                   data=obs, evidence=[shot])
        return ToolResult.success(obs, evidence=[shot])


class BrowserSignIn(Tool):
    name = "browser_sign_in"
    description = ("Sign in to a web system with its stored credentials. Use it on a sign-in page (or when told "
                   "the session expired); if the current page isn't a sign-in page, the system's start page is "
                   "opened first. Afterwards the system usually returns you to the page you were trying to reach.")
    parameters = {
        "type": "object",
        "properties": {"system": {"type": "string", "description": "Which web system (only needed if there are several)"}},
        "required": [],
    }
    changes_view = True  # read-only: authenticating changes no business data

    def run(self, ctx, system: str | None = None) -> ToolResult:
        system = system or ctx.config.default_web_system()
        if system is None:
            return ToolResult.fail("validation", f"Say which system. Web systems: {list(ctx.config.web_systems())}")
        session = ctx.browser
        page = session.ensure()
        if not on_login_form(page):
            opened = BrowserNavigate().run(ctx, url="/", system=system)
            if not on_login_form(page):
                if opened.ok:
                    session.logged_in = True
                    return ToolResult.success(opened.data, message=f"Already signed in to {system}.")
                return opened

        username, password = credentials_for(ctx, system)
        form = page.locator("form:has(input[type=password])").first
        form.locator("input:not([type=password]):not([type=hidden]):not([type=submit]):not([type=checkbox])").first.fill(username)
        form.locator("input[type=password]").first.fill(password)
        session.nav_log.clear()
        try:
            with page.expect_navigation(wait_until="load", timeout=NAV_TIMEOUT_MS):
                form.evaluate("f => f.requestSubmit()")
        except PlaywrightTimeout:
            return ToolResult.fail("transient", "Sign-in timed out. Try again.", data=observe(session))
        if failure := _settle_server_errors(session):
            return failure
        if on_login_form(page):
            obs = observe(session)
            return ToolResult.fail("auth", "Sign-in was rejected: " + (" ".join(obs["alerts"]) or "still on the sign-in page")
                                   + ". The stored credentials may be wrong; ask a human.", data=obs)
        session.logged_in = True
        return ToolResult.success(observe(session), message=f"Signed in to {system}.")


class BrowserFollow(Tool):
    name = "browser_follow"
    description = ("Follow a link, or press the button of a search form, by its name as shown in the snapshot. "
                   "It never submits changes: forms that would change data are refused. Use it to move around and "
                   "search when you only need to read.")
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Accessible name of the link or search button"},
            "role": {"type": "string", "enum": ["link", "button"], "description": "Default: link"},
        },
        "required": ["name"],
    }
    changes_view = True  # read-only: links and search forms only

    def run(self, ctx, name: str, role: str = "link") -> ToolResult:
        if failure := _require_page(ctx):
            return failure
        session = ctx.browser
        page = session.ensure()
        target, failure = _locate(page, role, name)
        if failure:
            return failure
        method = target.evaluate("el => el.form ? (el.form.getAttribute('method') || 'get').toLowerCase() : null")
        if method not in (None, "get"):
            return ToolResult.fail("policy_denied", f"'{name}' submits a form that changes data; that is not allowed "
                                                    "with browser_follow. Navigate or search instead.")
        return BrowserClick._perform(session, page, target, role, name, is_submit=False)


class BrowserFill(Tool):
    name = "browser_fill"
    description = ("Type a value into the form field with the given label (selects are chosen by option text). "
                   "Dates use YYYY-MM-DD. For passwords, pass the '$secret:...' reference from get_credentials "
                   "exactly as given; it is resolved securely and never shown.")
    parameters = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "The field's visible label"},
            "value": {"type": "string"},
        },
        "required": ["label", "value"],
    }
    # Read-only: typing changes nothing on the server until something is submitted.

    def run(self, ctx, label: str, value: str) -> ToolResult:
        if failure := _require_page(ctx):
            return failure
        page = ctx.browser.ensure()
        field = _find_field(page, label)
        if field is None:
            return ToolResult.fail("not_found", f"No field labelled '{label}' on this page.",
                                   data=_visible_controls(page))
        real_value, is_secret = resolve_secret(ctx, value)
        is_password = field.get_attribute("type") == "password"
        try:
            if field.evaluate("el => el.tagName") == "SELECT":
                field.select_option(label=real_value)
            else:
                field.fill(real_value)
        except PlaywrightError as exc:
            reason = str(exc).splitlines()[0]
            hint = " Dates must be YYYY-MM-DD." if "Malformed value" in reason else ""
            if "not editable" in reason:
                hint = " This field is read-only."
            return ToolResult.fail("validation", f"Could not fill '{label}': {reason}.{hint}")
        shown = "***" if is_secret or is_password else value
        return ToolResult.success({"field": label, "value": shown})


class BrowserSelect(Tool):
    name = "browser_select"
    description = "Choose an option in a dropdown (select) field by the option's visible text."
    parameters = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "The dropdown's visible label"},
            "option": {"type": "string", "description": "Visible text of the option to choose"},
        },
        "required": ["label", "option"],
    }
    # Read-only: typing changes nothing on the server until something is submitted.

    def run(self, ctx, label: str, option: str) -> ToolResult:
        if failure := _require_page(ctx):
            return failure
        page = ctx.browser.ensure()
        field = _find_field(page, label)
        if field is None:
            return ToolResult.fail("not_found", f"No dropdown labelled '{label}' on this page.",
                                   data=_visible_controls(page))
        options = [o.strip() for o in field.locator("option").all_inner_texts()]
        exact = [o for o in options if o == option]
        partial = [o for o in options if option.lower() in o.lower()]
        chosen = exact[0] if exact else (partial[0] if len(partial) == 1 else None)
        if chosen is None:
            return ToolResult.fail("not_found", f"No single option matching '{option}' in '{label}'.",
                                   data={"options": options})
        field.select_option(label=chosen)
        return ToolResult.success({"field": label, "selected": chosen})


class BrowserScreenshot(Tool):
    name = "browser_screenshot"
    description = "Save a screenshot of the current page as evidence. Returns the file path."
    parameters = {
        "type": "object",
        "properties": {"note": {"type": "string", "description": "What this screenshot shows"}},
        "required": ["note"],
    }

    def run(self, ctx, note: str) -> ToolResult:
        if failure := _require_page(ctx):
            return failure
        path = ctx.browser.screenshot(note)
        return ToolResult.success({"path": path, "url": ctx.browser.page.url}, evidence=[path])


def _find_field(page: Page, label: str) -> Locator | None:
    for locator in (page.get_by_label(label, exact=True), page.get_by_label(label), page.get_by_placeholder(label)):
        if locator.count() >= 1:
            return locator.first
    return None


BROWSER_TOOLS = [BrowserNavigate, BrowserSnapshot, BrowserSignIn, BrowserFollow, BrowserClick, BrowserFill,
                 BrowserSelect, BrowserScreenshot]
