"""Browser smoke test for the control interface.

Skipped unless Playwright and its Chromium build are present, so a fresh clone
still runs the rest of the suite without a 115 MB download. To enable:

    uv sync --all-extras
    uv run playwright install chromium

Note for Fedora/RHEL: do NOT pass --with-deps. That flag shells out to
apt-get, which does not exist here; the required libraries are already present
on a Fedora desktop.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, sync_playwright  # noqa: E402


PASSWORD = "smoke-admin-password-33"
SEED_KEY = "smoke-key-000000000000"
HOME = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:  # browser not downloaded
            pytest.skip(f"Chromium unavailable: {exc}")
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp = tmp_path_factory.mktemp("ui")
    port = free_port()
    environment = {
        **os.environ,
        "CONTROL_API_KEYS": SEED_KEY,
        "CONTROL_ADMIN_TOKEN": PASSWORD,
        "CREDENTIALS_PATH": str(tmp / "credentials.json"),
        "ENGRAI_ROUTE_DIR": str(tmp / "commands"),
        "ENGRAI_TEXT_ENGINE_LOG": str(tmp / "text-worker.log"),
        "MODEL_STATE_PATH": str(tmp / "runtime.json"),
        "ENGRAI_TEXT_ENGINE_PORT": str(free_port()),
    }
    process = subprocess.Popen(
        ["uv", "run", "uvicorn", "engrai_server.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=HOME,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("gateway exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("gateway did not start")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=15)


@pytest.fixture(scope="module")
def unconfigured_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A gateway with no credential store and no environment seed."""
    tmp = tmp_path_factory.mktemp("setup")
    port = free_port()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CONTROL_API_KEYS", "CONTROL_ADMIN_TOKEN"}
    }
    environment.update(
        {
            "CREDENTIALS_PATH": str(tmp / "credentials.json"),
            "ENGRAI_ROUTE_DIR": str(tmp / "commands"),
            "ENGRAI_TEXT_ENGINE_LOG": str(tmp / "text-worker.log"),
            "MODEL_STATE_PATH": str(tmp / "runtime.json"),
            "ENGRAI_TEXT_ENGINE_PORT": str(free_port()),
        }
    )
    process = subprocess.Popen(
        ["uv", "run", "uvicorn", "engrai_server.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=HOME,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("unconfigured gateway exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("unconfigured gateway did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        process.wait(timeout=15)


def _record_console(errors: list[str], message) -> None:
    """Collect real script errors, ignoring expected HTTP failures.

    Tests that exercise a rejected login or a refused revoke produce a 4xx,
    which Chrome logs as a resource error. That is the code under test working,
    not a fault.
    """
    if message.type != "error":
        return
    if "Failed to load resource" in message.text:
        return
    errors.append(message.text)



def sign_in(page: Page) -> None:
    page.get_by_test_id("unlock-password").fill(PASSWORD)
    page.locator('[data-testid="unlock-form"] button[type=submit]').click()
    # The hero replaces the gate, so its presence is the signal that the
    # session was accepted and the first snapshot has landed.
    page.wait_for_selector(".hero", timeout=15000)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_gguf_browser_selects_exact_variant_and_all_is_opt_in(page: Page, width: int):
    from playwright.sync_api import expect

    page.set_viewport_size({"width": width, "height": 900})
    repo = "example/a-long-model-name-GGUF"
    sha = "c" * 40
    q4 = ["model-Q4_K_M-00001-of-00002.gguf", "model-Q4_K_M-00002-of-00002.gguf"]
    q8 = ["model-Q8_0.gguf"]
    previews = []
    downloads = []
    page.route("**/control/model-browser/search?*", lambda route: route.fulfill(json={
        "models": [{"repo_id": repo, "downloads": 1000}],
    }))
    page.route("**/control/model-browser/variants?*", lambda route: route.fulfill(json={
        "repo_id": repo, "commit_hash": sha, "variants": [
            {"id": "q4", "quantization": "Q4_K_M", "files": q4, "bytes_total": 200, "complete": True},
            {"id": "q8", "quantization": "Q8_0", "files": q8, "bytes_total": 400, "complete": True},
        ],
    }))
    def preview(route):
        body = route.request.post_data_json
        previews.append(body)
        route.fulfill(json={
            "repo_id": repo, "requested_revision": sha, "commit_hash": sha,
            "files": [{"filename": name, "size": 100, "cached": False, "will_download": True} for name in body["filenames"]],
            "files_total": len(body["filenames"]), "bytes_total": 600, "bytes_to_download": 600,
            "free_bytes": 10000, "destination": "/tmp/test-hub",
        })
    page.route("**/control/model-pulls/preview", preview)
    def pulls(route):
        if route.request.method == "POST":
            downloads.append(route.request.post_data_json)
            route.fulfill(json={"task": {"id": "test"}})
        else:
            route.fulfill(json={"destination": "/tmp/test-hub", "authenticated": False, "active": None, "tasks": []})
    page.route("**/control/model-pulls", pulls)
    sign_in(page)
    panel = page.get_by_test_id("model-puller")
    expect(panel.get_by_test_id("pull-patterns")).to_have_count(0)
    panel.get_by_test_id("hub-search").fill("model")
    panel.get_by_role("button", name="Search", exact=True).click()
    panel.get_by_role("button", name=repo).click()
    quant = panel.get_by_test_id("hub-quant")
    expect(quant).to_be_enabled()
    expect(quant).to_have_value("")
    expect(panel.get_by_role("button", name="Review download")).to_be_disabled()
    quant.select_option("q4")
    panel.get_by_role("button", name="Review download").click()
    expect(panel.get_by_test_id("pull-preview")).to_be_visible()
    assert previews[-1]["filenames"] == q4
    assert previews[-1]["revision"] == sha
    assert not downloads
    quant.select_option("all")
    expect(panel.get_by_test_id("pull-preview")).to_have_count(0)
    expect(panel.locator(".pull-warning")).to_be_visible()
    panel.get_by_role("button", name="Review download").click()
    expect(panel.get_by_test_id("pull-preview")).to_be_visible()
    panel.get_by_role("button", name="Confirm download").click()
    expect(panel.get_by_test_id("pull-preview")).to_have_count(0)
    assert downloads[-1]["filenames"] == q4 + q8
    assert downloads[-1]["revision"] == sha
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    if width == 390:
        panel.screenshot(path="/tmp/engrai-gguf-browser-mobile.png")
    panel.get_by_role("button", name="Advanced:").click()
    expect(panel.get_by_test_id("pull-patterns")).to_be_visible()


def test_gguf_browser_discards_late_search_results(page: Page):
    from playwright.sync_api import expect

    pending = []
    page.route("**/control/model-browser/search?*", lambda route: pending.append(route))
    sign_in(page)
    panel = page.get_by_test_id("model-puller")
    search = panel.get_by_test_id("hub-search")
    search.fill("old model")
    with page.expect_request("**/control/model-browser/search?*"):
        panel.get_by_role("button", name="Search", exact=True).click()
    search.fill("new model")
    assert pending
    pending[0].fulfill(json={"models": [{"repo_id": "old/model", "downloads": 1}]})
    expect(panel.get_by_role("button", name="Search", exact=True)).to_be_enabled()
    # A subsequent browser round trip lets the fulfilled fetch settle.
    page.wait_for_timeout(100)
    expect(panel.locator(".pull-results")).to_have_count(0)
    expect(panel.get_by_test_id("hub-quant")).to_be_disabled()


def open_account(page: Page) -> None:
    sign_in(page)
    page.locator(".avatar").click()
    page.wait_for_selector(".account-block", timeout=10000)
    # The profile form is not rendered until its values have arrived, which is
    # what stops a late response overwriting a half-typed field. Waiting on the
    # field itself therefore also waits out that race.
    page.wait_for_selector('[data-testid="profile-name"]', timeout=10000)


@pytest.mark.parametrize("kind", ["account", "text", "image", "engine"])
def test_sheets_stay_within_viewport_with_long_content(page: Page, kind: str):
    from playwright.sync_api import expect

    long_name = "a-very-long-model-or-host-name-" * 18
    long_path = "/models/" + long_name + ".gguf"
    def discovery(route):
        model = {"path": long_path, "filename": long_name + ".gguf", "size_bytes": 1024, "size_gb": 0.1, "quantization": "Q4_K_M", "kind": "checkpoint"}
        route.fulfill(json={"models": [model], "files": [model], "library": {"model_search_roots": [long_path], "is_default": False, "default_roots": []}})
    page.route("**/control/discovery/models", discovery)
    page.route("**/control/discovery/image-files", discovery)
    sign_in(page)
    if kind == "account":
        page.locator(".avatar").click()
        page.get_by_test_id("profile-name").wait_for()
        # Exercise copyable URLs, long key labels, and diagnostic commands
        # without generating real credentials or changing user preferences.
        page.locator(".sheet .copy-field code").first.evaluate("(el, value) => el.textContent = value", "https://" + long_name + "/display")
        page.locator(".sheet .key-label").first.evaluate("(el, value) => el.textContent = value", long_name)
        page.locator(".sheet .check-list").evaluate("(el, value) => { const li = document.createElement('li'); const div = document.createElement('div'); const code = document.createElement('code'); code.textContent = value; div.append(code); li.append(div); el.append(li); }", long_path)
    elif kind == "text":
        page.locator(".panel.routes").first.get_by_role("button", name="New Profile", exact=True).click()
        page.get_by_test_id("model-select").select_option(long_path)
        page.locator(".sheet details").evaluate("el => el.open = true")
        page.get_by_test_id("extra-arguments").fill("--example=" + long_name)
    elif kind == "image":
        page.get_by_test_id("image-models").get_by_role("button", name="New Profile", exact=True).click()
        page.get_by_test_id("image-model-select").select_option(long_path)
    else:
        page.locator(".engine-pill").click()
        page.locator(".sheet .stat-rows dd").first.evaluate("(el, value) => el.textContent = value", long_name)
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    original_title = dialog.locator("h2").first.text_content()
    dialog.locator("h2").first.evaluate("(el, value) => el.textContent = value", long_name)
    for width, height in [(w, 640) for w in (1440, 1024, 721, 720, 640, 390, 320, 1440)] + [(844, 390), (320, 480)]:
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(100)
        problems = dialog.evaluate("""sheet => {
            const problems = [];
            const box = sheet.getBoundingClientRect();
            if (box.left < -1 || box.right > innerWidth + 1) problems.push('sheet outside viewport');
            for (const el of [sheet, ...sheet.querySelectorAll('*')]) {
                if (!el.getClientRects().length || el.matches('input, textarea, select, option, svg, svg *')) continue;
                if (el.clientWidth && el.scrollWidth > el.clientWidth + 1) problems.push(el.className || el.tagName);
            }
            const body = sheet.querySelector('.sheet-body');
            if (body.clientHeight < 44) problems.push('body has no usable height');
            for (const el of sheet.querySelectorAll('input, textarea, select, button')) {
                if (!el.getClientRects().length) continue;
                const control = el.getBoundingClientRect();
                if (control.left < box.left - 1 || control.right > box.right + 1) problems.push('control clipped horizontally');
            }
            for (const el of sheet.querySelectorAll('.sheet-head button, .sheet-foot')) {
                const fixed = el.getBoundingClientRect();
                if (fixed.top < -1 || fixed.bottom > innerHeight + 1) problems.push('header/footer outside viewport');
            }
            body.scrollLeft = 100;
            if (body.scrollLeft !== 0) problems.push('body scrolls sideways');
            body.scrollTop = body.scrollHeight;
            if (body.scrollHeight > body.clientHeight + 1 && body.scrollTop === 0) problems.push('vertical scroll blocked');
            return problems;
        }""")
        assert not problems, f"{kind} at {width}px: {problems}"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    dialog.locator("h2").first.evaluate("(el, value) => el.textContent = value", original_title)
    if kind in {"text", "image"}:
        for width in (390, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            dialog.locator(".sheet-body").evaluate("el => el.scrollTop = 0")
            dialog.screenshot(path=f"/tmp/engrai-sheet-{kind}-{width}.png")
    dialog.locator(".sheet-head button").click()
    expect(dialog).to_have_count(0)


def open_new_profile(page: Page) -> None:
    sign_in(page)
    page.get_by_role("button", name="New route").click()
    page.wait_for_selector('[data-testid="model-select"] option', state="attached", timeout=15000)


# ── First run ─────────────────────────────────────────────────────────────


def test_first_run_shows_setup_and_creates_an_account(browser, unconfigured_url: str) -> None:
    context = browser.new_context()
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"uncaught: {exc}"))
    page.on("console", lambda message: _record_console(errors, message))
    try:
        page.goto(unconfigured_url, wait_until="networkidle")
        # An unconfigured install offers setup, never a sign-in prompt.
        page.wait_for_selector('[data-testid="setup-form"]', state="visible", timeout=15000)
        assert page.locator('[data-testid="unlock-form"]').count() == 0

        page.get_by_test_id("setup-name").fill("Ada Lovelace")
        page.get_by_test_id("setup-email").fill("ada@example.com")
        page.get_by_test_id("setup-password").fill("a-long-enough-password")
        page.get_by_test_id("setup-confirm").fill("mismatched-password")
        page.locator('[data-testid="setup-form"] button[type=submit]').click()
        page.wait_for_selector(".error-note", timeout=10000)
        assert "do not match" in (page.text_content(".error-note") or "")

        page.get_by_test_id("setup-confirm").fill("a-long-enough-password")
        page.locator('[data-testid="setup-form"] button[type=submit]').click()
        page.wait_for_selector(".hero", timeout=15000)

        # The avatar takes its monogram from the name just entered.
        page.wait_for_function(
            "document.querySelector('.avatar').textContent.trim() === 'AL'",
            timeout=10000,
        )
        assert page.locator('[data-testid="setup-form"]').count() == 0
    finally:
        context.close()
    assert not errors, f"console errors: {errors}"


def test_setup_is_refused_once_configured(page: Page, base_url: str) -> None:
    status = page.evaluate("fetch('/control/status').then(r => r.json())")
    assert status["initialized"] is True
    code = page.evaluate(
        """fetch('/control/setup', {
             method: 'POST',
             headers: {'Content-Type': 'application/json'},
             body: JSON.stringify({name: 'Intruder', password: 'a-long-enough-password'}),
           }).then(r => r.status)"""
    )
    assert code == 409


@pytest.fixture
def page(browser, base_url: str) -> Iterator[Page]:
    context = browser.new_context()
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"uncaught: {exc}"))
    page.on("console", lambda message: _record_console(errors, message))
    page.goto(base_url, wait_until="networkidle")
    yield page
    context.close()
    assert not errors, f"console errors: {errors}"


# ── Sign in ───────────────────────────────────────────────────────────────


def test_password_signs_in_and_replaces_the_gate(page: Page) -> None:
    sign_in(page)
    assert page.locator('[data-testid="unlock-form"]').count() == 0


def test_wrong_password_is_reported(page: Page) -> None:
    page.get_by_test_id("unlock-password").fill("definitely-not-the-password")
    page.locator('[data-testid="unlock-form"] button[type=submit]').click()
    page.wait_for_selector(".error-note", timeout=10000)
    assert "Invalid password" in (page.text_content(".error-note") or "")
    assert page.locator('[data-testid="unlock-form"]').is_visible()


# ── Layout ────────────────────────────────────────────────────────────────


def test_unchanged_polls_stay_idle_and_telemetry_still_updates(page: Page) -> None:
    # Count React commits, since identical DOM does not prove React skipped
    # rendering (and the old layout animations measured even unchanged DOM).
    page.add_init_script("""window.__commits = 0;
        window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
            supportsFiber: true,
            inject: () => 1,
            onCommitFiberRoot: () => window.__commits++,
            onCommitFiberUnmount: () => {},
        };""")
    page.reload(wait_until="networkidle")
    frozen = None

    def state(route):
        nonlocal frozen
        if frozen is None:
            frozen = route.fetch().json()
        route.fulfill(json=frozen)

    metrics = {"cpu": {"percent": 25}, "memory": None, "gpus": []}
    page.route("**/control/state", state)
    page.route("**/control/metrics", lambda route: route.fulfill(json=metrics))
    sign_in(page)
    page.wait_for_function("document.querySelector('.meter-value')?.textContent === '25%'")
    # Let the entrance finish and align to a completed polling cycle.
    with page.expect_response("**/control/metrics", timeout=10000):
        page.wait_for_timeout(5100)
    page.wait_for_timeout(300)
    commits = page.evaluate("window.__commits")
    assert commits > 0, "React commit instrumentation did not attach"
    with page.expect_response("**/control/metrics", timeout=10000):
        page.wait_for_timeout(5100)
    assert page.evaluate("window.__commits") == commits

    metrics["cpu"]["percent"] = 80
    page.wait_for_function("document.querySelector('.meter-value')?.textContent === '80%'", timeout=10000)
    # The fill changes visually while its layout width remains the full track.
    page.wait_for_function("""() => {
        const fill = document.querySelector('.meter-fill');
        const track = fill.parentElement;
        return Math.abs(fill.getBoundingClientRect().width / track.getBoundingClientRect().width - .8) < .01 &&
            fill.offsetWidth === track.clientWidth;
    }""")
    assert page.locator(".meter-hot").count() == 1
    # Non-telemetry changes must also reach the dashboard.
    frozen["engine"]["version"] = "scroll-check"
    page.wait_for_function("document.querySelector('.status-rail').textContent.includes('scroll-check')", timeout=10000)


def test_header_is_sticky_at_the_top(page: Page) -> None:
    sign_in(page)
    header = page.locator(".topbar")
    assert header.evaluate("element => getComputedStyle(element).position") == "sticky"
    assert header.evaluate("element => getComputedStyle(element).top") == "0px"


@pytest.mark.parametrize("catalog", ["empty", "active", "inactive"])
def test_control_resizes_without_page_overflow(page: Page, catalog: str) -> None:
    """Wide content must scroll inside its panel, without widening the page."""
    if catalog != "empty":
        def state(route):
            response = route.fetch()
            snapshot = response.json()
            model = {
                "id": "a-long-model-deployment-" * 6,
                "name": "A long model name",
                "active": catalog == "active",
                "routable": True,
                "missing_paths": [],
                "quantization": "Q4_K_M",
                "size_gb": 8,
                "context_tokens": 32768,
                "gpu_layers": 32,
                "gpu": 1,
                "vram_limit_mib": 16384,
                "notes": "https://example.com/" + "model-notes-" * 20,
            }
            snapshot.update(models=[model], image_models=[model])
            route.fulfill(response=response, json=snapshot)

        page.route("**/control/state", state)

    sign_in(page)
    page.wait_for_selector(".meters")
    # Resize both ways, including the narrowest supported phone and the
    # desktop/mobile breakpoint where the status rail changes layout.
    for width in (1440, 1024, 768, 721, 720, 390, 320, 390, 1440):
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_function(
            """() => document.documentElement.scrollWidth <= innerWidth &&
                document.body.scrollWidth <= innerWidth""",
            timeout=3000,
        )
        assert page.locator(".topbar").evaluate(
            "element => element.getBoundingClientRect().width"
        ) == width
        for selector in (".status-rail", ".content > .meters"):
            # Instruments keep their height and spread across each row,
            # including the final row when there are fewer items in it.
            assert page.locator(selector).evaluate(
                """element => {
                    const rail = element.getBoundingClientRect();
                    const rows = new Map();
                    for (const child of element.children) {
                        const box = child.getBoundingClientRect();
                        if (box.height !== 80) return false;
                        const row = rows.get(box.top) ?? [];
                        row.push(box);
                        rows.set(box.top, row);
                    }
                    return element.scrollWidth <= element.clientWidth &&
                        [...rows.values()].every(row =>
                            Math.abs(row[0].left - rail.left) < 1 &&
                            Math.abs(row.at(-1).right - rail.right) < 1);
                }"""
            )
        if catalog != "empty":
            assert page.locator(".code-block").evaluate(
                "element => element.scrollWidth > element.clientWidth"
            )


def test_mobile_engine_status_opens_a_sheet(page: Page) -> None:
    """The status detail used to ride on the `popover` attribute.

    That attribute does nothing on iOS 16, which is still shipping on iPads
    plausible as wall displays, so it is a scripted sheet now. The status dot
    must survive the label being hidden — both are spans.
    """
    sign_in(page)
    page.set_viewport_size({"width": 390, "height": 844})
    header_box = page.locator(".topbar").bounding_box()
    brand_box = page.locator(".brand").bounding_box()
    actions_box = page.locator(".topbar-actions").bounding_box()
    assert header_box and header_box["height"] < 80
    assert brand_box and actions_box
    assert abs(brand_box["y"] - actions_box["y"]) < 20

    assert page.locator(".engine-pill .dot").is_visible()
    page.locator(".engine-pill").click()
    page.wait_for_selector('.sheet[role="dialog"]', timeout=10000)
    assert page.locator(".stat-rows").is_visible()


def test_every_touch_target_meets_the_minimum(page: Page) -> None:
    """44px is the floor for a finger; the topbar meets it with hit padding."""
    sign_in(page)
    page.set_viewport_size({"width": 390, "height": 844})
    # This gateway starts with no routes, so the panel — not a card — is the
    # signal that the page has finished laying out.
    page.wait_for_selector(".panel.routes")
    undersized = page.evaluate(
        """() => [...document.querySelectorAll('button, a, select, input')]
             .map(element => {
               const box = element.getBoundingClientRect();
               if (!box.width) return null;
               const after = getComputedStyle(element, '::after');
               const height = Math.max(box.height, parseFloat(after.height) || 0);
               const width = Math.max(box.width, parseFloat(after.width) || 0);
               return (height < 44 || width < 44)
                 ? `${element.className || element.tagName} ${Math.round(height)}x${Math.round(width)}`
                 : null;
             })
             .filter(Boolean)"""
    )
    assert not undersized, f"touch targets under 44px: {undersized}"


# ── Account panel ─────────────────────────────────────────────────────────


def test_avatar_opens_the_account_panel(page: Page) -> None:
    open_account(page)
    assert "Profile" in (page.text_content('.sheet[role="dialog"] h2') or "")
    page.wait_for_selector('[data-testid="api-keys"] .key-list li')
    page.wait_for_selector(".check-list li")


def test_last_key_cannot_be_revoked(page: Page) -> None:
    open_account(page)
    page.wait_for_selector('[data-testid="api-keys"] .key-list li')
    assert page.locator('[data-testid="api-keys"] .key-list li').count() == 1
    assert page.locator('[data-testid="api-keys"] [data-testid="revoke-key"]').first.is_disabled()


def test_generating_a_key_reveals_it_once_and_refreshes_the_list(page: Page) -> None:
    open_account(page)
    page.wait_for_selector('[data-testid="api-keys"] .key-list li')
    page.get_by_test_id("key-label").fill("smoke key")
    page.get_by_test_id("mint-key").click()
    page.wait_for_selector('[data-testid="api-keys"] .reveal', state="visible", timeout=10000)

    secret = (page.text_content('[data-testid="api-keys"] .reveal code') or "").strip()
    assert secret.startswith("eng-")
    # A CSS wait beats escaping a selector through Python into JavaScript.
    page.wait_for_selector('[data-testid="api-keys"] .key-list li:nth-child(2)')
    assert "smoke key" in page.locator('[data-testid="api-keys"] .key-label').all_text_contents()
    # A second key means the first is now revocable.
    assert not page.locator('[data-testid="api-keys"] [data-testid="revoke-key"]').first.is_disabled()

    page.locator('.sheet[role="dialog"] .icon-button').click()
    page.locator(".avatar").click()
    page.wait_for_selector('[data-testid="api-keys"] .key-list li')
    assert page.locator('[data-testid="api-keys"] .reveal').count() == 0, "the plaintext key must not persist"


def test_a_late_response_cannot_overwrite_a_half_typed_field(page: Page) -> None:
    """The regression behind commit efcec74, closed from both directions.

    The poll no longer rebuilds the DOM, and the credential fetch can no longer
    seed a field that is already on screen: the form is not rendered until its
    values are in hand.
    """
    typed = "a half-typed name"
    sign_in(page)
    page.locator(".avatar").click()
    page.wait_for_selector('[data-testid="profile-name"]', timeout=10000)
    page.get_by_test_id("profile-name").fill(typed)
    # Three idle poll cycles at five seconds each, plus margin.
    page.wait_for_timeout(12000)
    assert page.get_by_test_id("profile-name").input_value() == typed
    assert page.evaluate(
        "document.activeElement === document.querySelector('[data-testid=\"profile-name\"]')"
    )


def test_changing_the_password_keeps_this_session_alive(page: Page) -> None:
    open_account(page)
    page.get_by_test_id("current-password").fill(PASSWORD)
    page.get_by_test_id("new-password").fill("rotated-in-a-browser")
    page.get_by_test_id("confirm-password").fill("rotated-in-a-browser")
    page.get_by_role("button", name="Change password").click()
    page.wait_for_function(
        "[...document.querySelectorAll('.activity-list span')]"
        ".some(node => node.textContent.toLowerCase().includes('password changed'))",
        timeout=10000,
    )
    # The poll must succeed on the replacement token rather than locking us out.
    page.wait_for_timeout(6000)
    assert page.locator('[data-testid="unlock-form"]').count() == 0

    # The gateway is shared across this module, so restore the seed password.
    page.get_by_test_id("current-password").fill("rotated-in-a-browser")
    page.get_by_test_id("new-password").fill(PASSWORD)
    page.get_by_test_id("confirm-password").fill(PASSWORD)
    page.get_by_role("button", name="Change password").click()
    page.wait_for_function(
        "[...document.querySelectorAll('.activity-list span')]"
        ".filter(node => node.textContent.toLowerCase().includes('password changed')).length === 2",
        timeout=10000,
    )


def test_mismatched_confirmation_is_rejected_client_side(page: Page) -> None:
    open_account(page)
    page.get_by_test_id("current-password").fill(PASSWORD)
    page.get_by_test_id("new-password").fill("one-long-password")
    page.get_by_test_id("confirm-password").fill("another-long-password")
    page.get_by_role("button", name="Change password").click()
    page.wait_for_selector(".error-note", timeout=10000)
    assert "do not match" in (page.text_content(".error-note") or "")


def test_profile_edit_updates_the_avatar(page: Page) -> None:
    open_account(page)
    page.get_by_test_id("profile-name").fill("Grace Hopper")
    page.get_by_test_id("profile-email").fill("grace@example.com")
    page.get_by_role("button", name="Save profile").click()
    page.wait_for_function(
        "document.querySelector('.avatar').textContent.trim() === 'GH'",
        timeout=10000,
    )


# ── Status rail and host resources ───────────────────────────────────────


def test_control_shell_uses_the_compact_status_rail(page: Page) -> None:
    sign_in(page)
    page.wait_for_selector(".status-rail", timeout=15000)
    assert page.locator(".status-card").count() == 4
    # The rail reports state; the meters report headroom. Both are on the
    # control view — whether the next model fits is a VRAM question, and the
    # rail cannot answer it.
    assert page.locator(".meters .meter").count() > 0

    labels = page.locator(".status-card .eyebrow").all_text_contents()
    assert labels == ["Current status", "Gateway", "Engine", "Lane"]

    unload = page.get_by_role("button", name="Unload active model")
    assert unload.count() == 1
    assert unload.locator("svg").count() == 1
    assert (unload.text_content() or "").strip() == ""


def test_resource_meters_render_after_sign_in(page: Page) -> None:
    sign_in(page)
    page.wait_for_selector(".meters .meter", timeout=15000)
    labels = page.locator(".meters .meter-label").all_text_contents()
    # CPU and RAM always exist; GPU entries depend on the host.
    assert "CPU" in labels
    assert "RAM" in labels
    for meter in page.locator(".meters .meter").all():
        width = meter.locator(".meter-fill").evaluate(
            "element => getComputedStyle(element).width"
        )
        assert width.endswith("px"), f"meter bar has no width: {width}"


def test_meters_clear_on_sign_out(page: Page) -> None:
    sign_in(page)
    # Wait on the meters themselves, not the rail: waiting on the rail would
    # let this pass without the meters ever having rendered.
    page.wait_for_selector(".meters .meter", timeout=15000)
    page.locator(".avatar").click()
    page.wait_for_selector('.sheet[role="dialog"]')
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_selector('[data-testid="unlock-form"]', timeout=10000)
    # Host figures must not linger behind the lock screen.
    assert page.locator(".meters .meter").count() == 0


# ── Route editor ──────────────────────────────────────────────────────────


def test_model_picker_needs_no_typing(page: Page) -> None:
    """A datalist matched on the option value, which had to be the full path.

    That meant typing the long cache prefix before anything appeared. A select
    lists everything up front, each option led by its filename so the list is
    scannable; the truncated path follows only to separate identically named
    files in different repositories.
    """
    open_new_profile(page)
    labels = page.locator('[data-testid="model-select"] option').all_text_contents()
    assert len(labels) > 1
    real = [label for label in labels if " — " in label]
    assert real, "no discovered models offered"
    for label in real:
        assert not label.startswith("/"), f"option leads with a path: {label}"
        assert label.split(" — ")[0].endswith(".gguf"), f"option is not filename-led: {label}"


def test_route_id_pattern_compiles(page: Page) -> None:
    """The pattern attribute is compiled in v-mode, where '-' needs escaping.

    An invalid pattern is dropped silently, taking the field's validation with
    it, so this asserts the browser actually accepted it.
    """
    open_new_profile(page)
    valid = page.evaluate(
        "() => { const el = document.querySelector('[data-testid=\"route-id\"]');"
        " el.value = 'my-model-q4'; return el.checkValidity(); }"
    )
    assert valid is True
    invalid = page.evaluate(
        "() => { const el = document.querySelector('[data-testid=\"route-id\"]');"
        " el.value = '-leading-dash'; return el.checkValidity(); }"
    )
    assert invalid is False


def test_advanced_arguments_start_empty(page: Page) -> None:
    open_new_profile(page)
    assert page.get_by_test_id("extra-arguments").input_value() == ""


def test_text_deployment_uses_typed_controls(page: Page) -> None:
    open_new_profile(page)
    assert page.get_by_test_id("backend").input_value() == "auto"
    assert page.get_by_test_id("context").input_value() == "32768"
    assert page.get_by_test_id("kv-cache").input_value() == "q8_0"


def test_search_directory_is_shown_and_editable(page: Page) -> None:
    open_new_profile(page)
    assert page.get_by_test_id("search-roots").input_value()
    hints = page.locator(".field-hint").all_text_contents()
    assert any("GGUF files found on this host" in hint for hint in hints), hints


# ── Wall display ──────────────────────────────────────────────────────────


def test_display_mode_renders_and_offers_no_controls(page: Page, base_url: str) -> None:
    """The wall view is read-only by construction.

    A monitor in a shared room must not let anyone walking past it unload a
    model, so the display route renders no control that mutates the machine.
    """
    sign_in(page)
    page.goto(f"{base_url}/display", wait_until="networkidle")
    page.wait_for_selector(".display-name", timeout=15000)
    assert page.locator(".meters .meter").count() > 0
    for name in ("Load", "Unload active model", "New route", "Sign out"):
        assert page.get_by_role("button", name=name).count() == 0


# ── Version skew ──────────────────────────────────────────────────────────


def _pretend_a_new_build_is_deployed(page: Page) -> None:
    """Fake the endpoint rather than rewrite src/engrai_server/static.

    The running gateway reads that directory per request, so editing it here
    would briefly serve a broken shell to anything else pointed at this
    checkout — including a live deployment.
    """
    page.route(
        "**/control/version",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"assets": ["index-NEWBUILD.js", "index-NEWBUILD.css"]}',
        ),
    )
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def test_an_upgrade_waits_while_a_sheet_is_open(page: Page) -> None:
    """Reloading out from under someone mid-edit is its own bug."""
    sign_in(page)
    page.locator(".avatar").click()
    page.wait_for_selector('[data-testid="profile-name"]', timeout=10000)

    _pretend_a_new_build_is_deployed(page)

    page.wait_for_selector(".update-bar", timeout=10000)
    assert "newer version is available" in (page.text_content(".update-bar") or "")
    # Still on the same page: the sheet was not torn away mid-edit.
    assert page.locator('[data-testid="profile-name"]').count() == 1


def test_a_pinned_build_stops_reloading_and_says_so(page: Page) -> None:
    """The loop guard.

    If a reload was already attempted for this exact build and the page is
    still on the old one, something upstream is pinning it — a CDN edge
    holding the shell. Reloading again would spin forever, so it stops and
    tells a person instead.
    """
    sign_in(page)
    page.evaluate(
        "sessionStorage.setItem('engrai.reloadAttemptedFor',"
        " 'index-NEWBUILD.css,index-NEWBUILD.js')"
    )
    _pretend_a_new_build_is_deployed(page)

    page.wait_for_selector(".update-bar", timeout=10000)
    assert "cached copy is pinning it" in (page.text_content(".update-bar") or "")
    # Crucially, it did not navigate: the hero is still the one we signed into.
    assert page.locator(".status-rail").count() == 1


def test_no_notice_when_the_build_matches(page: Page) -> None:
    """The common case must stay silent."""
    sign_in(page)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(1500)
    assert page.locator(".update-bar").count() == 0
