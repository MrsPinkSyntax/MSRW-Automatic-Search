import asyncio
import json
import os
import random
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

# ======================
# CONFIG
# ======================

EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9222
CDP_ADDR = "127.0.0.1"

# Se vuoi usare i PROFILI REALI di Edge, lascia così:
REAL_EDGE_USER_DATA_DIR = os.path.join(os.environ["LOCALAPPDATA"], r"Microsoft\Edge\User Data")

# Se invece vuoi una sessione "pulita" dedicata all'automazione (più stabile),
# commenta la riga sopra e usa questa:
# REAL_EDGE_USER_DATA_DIR = os.path.join(os.environ["LOCALAPPDATA"], r"EdgeCDP_Automation")

QUERIES_FILE = "query.txt"

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

# ======================
# UTILS
# ======================

async def sleepy(a: float, b: float):
    await asyncio.sleep(random.uniform(a, b))

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

def cdp_version_url(port: int) -> str:
    return f"http://{CDP_ADDR}:{port}/json/version"

def wait_for_ws_url(port: int, timeout_s: float = 15.0) -> str:
    """Aspetta che Edge esponga CDP su /json/version e ritorna webSocketDebuggerUrl."""
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            with urlopen(cdp_version_url(port)) as r:
                data = json.loads(r.read().decode("utf-8"))
            ws = data.get("webSocketDebuggerUrl")
            if ws:
                return ws
        except Exception as e:
            last_err = e
        time.sleep(0.25)
    raise RuntimeError(f"CDP non pronto su porta {port}. Ultimo errore: {last_err}")

def launch_edge(profile_directory: str, start_url: str = "https://www.bing.com") -> subprocess.Popen:
    """
    Avvia Edge con CDP e un profilo specifico.
    Usa REAL_EDGE_USER_DATA_DIR come base dei profili (Default, Profile 1, ...).
    """
    args = [
        EDGE_EXE,
        f"--remote-debugging-port={PORT}",
        f"--remote-debugging-address={CDP_ADDR}",
        f'--user-data-dir={REAL_EDGE_USER_DATA_DIR}',
        f'--profile-directory={profile_directory}',
        start_url,
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def terminate_process(proc: subprocess.Popen, timeout_s: float = 8.0):
    """Chiude l'istanza Edge lanciata da noi."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout_s)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

# ======================
# PLAYWRIGHT ACTIONS
# ======================

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

    # fallback: prova ad aprire la search UI
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

async def get_or_make_page(ctx):
    if ctx.pages:
        return ctx.pages[0]
    return await ctx.new_page()

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

            await sleepy(PAUSE_MIN, PAUSE_MAX)

        except PWTimeoutError:
            print(f"[WARN {label}] Timeout per: {q} (URL: {page.url})")
            await sleepy(PAUSE_MIN, PAUSE_MAX)
            continue

async def run_profile(profile_directory: str, label: str, queries: list[str], n_searches: int, leave_open: bool):
    """
    Avvia Edge su quel profilo, si attacca via CDP, fa le ricerche, poi:
    - se leave_open=False chiude Playwright + termina il processo Edge
    - se leave_open=True chiude solo Playwright (stacca) e lascia Edge aperto
    """
    proc = launch_edge(profile_directory, "https://www.bing.com")
    try:
        ws = wait_for_ws_url(PORT, timeout_s=20.0)

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws)

            # In attach CDP di solito c'è un solo context persistente
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await get_or_make_page(ctx)

            await run_searches(label, page, queries, n_searches)

            # Stacchiamo Playwright
            await browser.close()

        if not leave_open:
            terminate_process(proc)
    except Exception:
        # se qualcosa va storto e dovevamo chiudere, chiudiamo comunque
        if not leave_open:
            terminate_process(proc)
        raise

# ======================
# MAIN
# ======================

async def main():
    default_n = ask_int("Quante ricerche sul profilo DEFAULT? (0 per saltare): ")
    prof1_n = ask_int("Quante ricerche sul profilo PROFILE 1? (0 per saltare): ")

    script_dir = Path(__file__).resolve().parent
    queries = load_queries(script_dir / QUERIES_FILE)

    # 1) Default: fa ricerche e CHIUDE Edge
    if default_n > 0:
        print("\n=== PROFILO: Default (chiusura a fine) ===")
        await run_profile("Default", "DEFAULT", queries, default_n, leave_open=False)
    else:
        print("\n[DEFAULT] Saltato.")

    # 2) Profile 1: fa ricerche e LASCIA Edge APERTO
    if prof1_n > 0:
        print("\n=== PROFILO: Profile 1 (resta aperto) ===")
        await run_profile("Profile 1", "PROFILE_1", queries, prof1_n, leave_open=True)
    else:
        print("\n[PROFILE 1] Saltato.")

    print("\nFine. (Se hai eseguito Profile 1, Edge rimane aperto.)")

if __name__ == "__main__":
    asyncio.run(main())
