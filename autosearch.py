import asyncio
import json
import random
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

CDP_VERSION_URL = "http://127.0.0.1:9222/json/version"
QUERIES_FILE = "query.txt"

# windows terminal

#> & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="$env:LOCALAPPDATA\Microsoft\Edge\User Data" --profile-directory="Profile 2" "https://www.bing.com"
#> taskkill /IM msedge.exe /F

SEARCH_BOX = (
    "#sb_form_q, input#sb_form_q, "
    "input[name='q'], textarea[name='q'], "
    "form[role='search'] input, form[role='search'] textarea, "
    "#b_searchboxForm input, #b_searchboxForm textarea"
)

PAUSE_MIN = 8.0
PAUSE_MAX = 16.0
TYPE_DELAY_MIN_MS = 60
TYPE_DELAY_MAX_MS = 120
MOBILE_METRICS = {
    "width": 390,
    "height": 844,
    "deviceScaleFactor": 3,
    "mobile": True
}
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

def sleepy(a: float, b: float):
    time.sleep(random.uniform(a, b))

def get_ws_url() -> str:
    with urlopen(CDP_VERSION_URL) as r:
        data = json.loads(r.read().decode("utf-8"))
    ws = data.get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError("webSocketDebuggerUrl non trovato. Avvia Edge con --remote-debugging-port=9222.")
    return ws

def load_queries(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Non trovo {path.name} in: {path.parent}")
    queries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        q = line.strip()
        if q and not q.startswith("#"):
            queries.append(q)
    if not queries:
        raise RuntimeError(f"{path.name} è vuoto.")
    return queries

def pick_queries(queries: list[str], count: int) -> list[str]:
    if count <= 0:
        return []
    if len(queries) >= count:
        return random.sample(queries, k=count)
    return [random.choice(queries) for _ in range(count)]

def ask_int(prompt: str) -> int:
    while True:
        s = input(prompt).strip()
        try:
            n = int(s)
            if n < 0:
                print("Inserisci un numero >= 0.")
                continue
            return n
        except ValueError:
            print("Inserisci un numero intero (es. 10).")

async def maybe_handle_cookies(page):
    await page.wait_for_timeout(400)

    reject_selectors = [
        "button:has-text('Rifiuta')",
        "button:has-text('Rifiuto')",
        "button:has-text('Reject')",
        "button:has-text('Decline')",
        "button[aria-label*='Reject' i]",
        "button[aria-label*='Rifiuta' i]",
    ]
    accept_selectors = [
        "button:has-text('Accetta')",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button[aria-label*='Accept' i]",
        "button[aria-label*='Accetta' i]",
    ]

    async def try_click(selectors):
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(500)
                    return True
            except Exception:
                pass
        return False

    if await try_click(reject_selectors):
        return
    await try_click(accept_selectors)

async def ensure_bing_ready(page):
    if not (page.url or "").startswith("https://www.bing.com"):
        await page.goto("https://www.bing.com", wait_until="domcontentloaded")

    await page.wait_for_load_state("domcontentloaded")
    await maybe_handle_cookies(page)

    async def get_box(timeout_ms=8000):
        box = page.locator(SEARCH_BOX).first
        await box.wait_for(state="visible", timeout=timeout_ms)
        return box

    try:
        return await get_box()
    except Exception:
        pass

    candidates = [
        "button[aria-label*='Search' i]",
        "a[aria-label*='Search' i]",
        "button:has-text('Search')",
        "button[title*='Search' i]",
        "#sbBtn",
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(300)
                break
        except Exception:
            pass

    await maybe_handle_cookies(page)
    try:
        return await get_box(12000)
    except Exception:
        await page.goto("https://www.bing.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(300)
        await maybe_handle_cookies(page)
        return await get_box(15000)

async def apply_mobile_emulation(page):
    """
    Apply mobile emulation *on the same logged-in profile* using CDP.
    """
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Emulation.setDeviceMetricsOverride", MOBILE_METRICS)
    await cdp.send("Emulation.setUserAgentOverride", {"userAgent": MOBILE_UA})
    await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    return cdp

async def clear_mobile_emulation(cdp):
    try:
        await cdp.send("Emulation.clearDeviceMetricsOverride")
    except Exception:
        pass
    try:
        await cdp.send("Emulation.setUserAgentOverride", {"userAgent": ""})
    except Exception:
        pass
    try:
        await cdp.send("Emulation.setTouchEmulationEnabled", {"enabled": False})
    except Exception:
        pass

async def run_searches(label: str, page, queries: list[str], count: int):
    chosen = pick_queries(queries, count)
    if not chosen:
        print(f"[{label}] Nessuna ricerca richiesta.")
        return

    await page.bring_to_front()

    for i, q in enumerate(chosen, start=1):
        try:
            before_url = page.url

            box = await ensure_bing_ready(page)
            await box.click()
            await box.fill("")
            await box.type(q, delay=random.randint(TYPE_DELAY_MIN_MS, TYPE_DELAY_MAX_MS))
            await box.press("Enter")

            try:
                await page.wait_for_selector("li.b_algo, #b_results", timeout=15000)
            except PWTimeoutError:
                pass

            after_url = page.url
            q_enc = quote_plus(q)
            expected_fragment = f"q={q_enc}"

            if (after_url == before_url) or ("bing.com/search" not in after_url) or (expected_fragment not in after_url):
                search_url = f"https://www.bing.com/search?q={q_enc}"
                await page.goto(search_url, wait_until="domcontentloaded")
                await page.wait_for_selector("li.b_algo, #b_results", timeout=30000)
                after_url = page.url

            changed = "OK" if after_url != before_url else "SAME_URL"
            print(f"[{label} {i}/{count}] {q} ({changed})")

            try:
                await page.mouse.wheel(0, random.randint(600, 1400))
            except Exception:
                pass

            sleepy(PAUSE_MIN, PAUSE_MAX)

        except PWTimeoutError:
            print(f"[WARN {label}] Timeout per: {q} (URL: {page.url})")
            sleepy(PAUSE_MIN, PAUSE_MAX)
            continue

async def main():
    desktop_n = ask_int("Quante ricerche DESKTOP? (0 per saltare): ")
    mobile_n = ask_int("Quante ricerche TELEFONO (emulazione nella stessa sessione)? (0 per saltare): ")

    script_dir = Path(__file__).resolve().parent
    queries = load_queries(script_dir / QUERIES_FILE)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(get_ws_url())
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        if desktop_n > 0:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await run_searches("DESKTOP", page, queries, desktop_n)
        if mobile_n > 0:
            mobile_page = await ctx.new_page()
            await mobile_page.goto("https://www.bing.com", wait_until="domcontentloaded")

            cdp = await apply_mobile_emulation(mobile_page)
            try:
                await mobile_page.reload(wait_until="domcontentloaded")
                await run_searches("TELEFONO_UI", mobile_page, queries, mobile_n)
            finally:
                await clear_mobile_emulation(cdp)
    print("Fine.")

if __name__ == "__main__":
    asyncio.run(main())


