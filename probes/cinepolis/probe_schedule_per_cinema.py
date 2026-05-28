"""
probe_schedule_per_cinema.py
============================
Verifikasi apakah pCinemaId benar-benar dipakai server,
atau server selalu return data cinema yang sama (session-bound).

Test:
  1. Ambil session dari Playwright (buka cinema A)
  2. POST loadScheduleTimesByDate untuk cinema A → catat film
  3. POST loadScheduleTimesByDate untuk cinema B (cinema berbeda) → catat film
  4. Bandingkan: apakah film berbeda?

Jika sama → server ignore pCinemaId, pakai session
Jika beda  → pCinemaId bekerja, masalah lain
"""
import asyncio
import json
from datetime import date
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

BASE    = "https://cinepolis.co.id"
OUT_DIR = Path("probe_output")
OUT_DIR.mkdir(exist_ok=True)

# Dua cinema yang BERBEDA KOTA — harusnya punya film berbeda
CINEMA_A = {"id": "525e66bd-4934-4314-aa66-62c2cdf3541d", "slug": "binjai-supermall",   "name": "Binjai Supermall"}
CINEMA_B = {"id": "c6d8218f-213b-49ef-bb86-19c12539706a", "slug": "cibubur-junction",   "name": "Cibubur Junction"}
CINEMA_C = {"id": "8bce6bde-4b4f-400d-8426-1964eacfbf5f", "slug": "citimall-sampit",    "name": "Citimall Sampit"}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def decode(text: str):
    try:
        outer = json.loads(text)
        inner = json.loads(outer.get("d", "{}"))
        return inner.get("DataObject")
    except Exception:
        return None

def extract_titles(schedule_html: str) -> list[str]:
    soup = BeautifulSoup(schedule_html, "lxml")
    titles = []
    for div in soup.select("div[id^='DivMovie_']"):
        head = div.select_one("div.head")
        if head:
            titles.append(head.get_text().strip())
    return sorted(titles)

def extract_showtimes(schedule_html: str) -> dict:
    """Return {title: [times]} dari schedule HTML."""
    soup   = BeautifulSoup(schedule_html, "lxml")
    result = {}
    for div in soup.select("div[id^='DivMovie_']"):
        head = div.select_one("div.head")
        if not head:
            continue
        title = head.get_text().strip()
        times = []
        for a in div.select("div.sch_date a"):
            t = a.get_text().strip()
            if ":" in t:
                times.append(t)
        result[title] = sorted(times)
    return result


async def get_schedule_httpx(
    client: httpx.AsyncClient,
    cinema: dict,
    session_cookie: str,
) -> tuple[str, dict]:
    """POST loadScheduleTimesByDate untuk satu cinema via httpx."""
    payload = {
        "pMovieId":     "0",
        "pCinemaId":    cinema["id"],
        "pScreenClass": "0",
        "pSelDate":     date.today().strftime("%d/%m/%Y"),
    }
    headers = {
        "User-Agent":      UA,
        "Content-Type":    "application/json; charset=UTF-8",
        "Accept":          "application/json, text/javascript, */*; q=0.01",
        "Origin":          BASE,
        "Referer":         f"{BASE}/movie/cinemaDetail.aspx?id={cinema['id']}&title={cinema['slug']}",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie":          session_cookie,
    }
    try:
        resp = await client.post(
            f"{BASE}/movie/cinemaDetail.aspx/loadScheduleTimesByDate",
            json=payload,
            headers=headers,
            timeout=20,
        )
        data = decode(resp.text)
        html = ""
        if isinstance(data, dict):
            html = data.get("strScheduleTimeData") or ""
        titles    = extract_titles(html)
        showtimes = extract_showtimes(html)
        return html, {"titles": titles, "showtimes": showtimes, "status": resp.status_code, "len": len(resp.text)}
    except Exception as e:
        return "", {"error": str(e)}


async def get_schedule_playwright_intercept(cinema: dict) -> tuple[str, str]:
    """
    Playwright buka cinemaDetail untuk cinema ini.
    Return (schedule_html, session_cookie).
    """
    from playwright.async_api import async_playwright

    schedule_html  = ""
    session_cookie = ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx  = await browser.new_context(user_agent=UA, locale="id-ID")
        page = await ctx.new_page()

        async def on_resp(resp):
            nonlocal schedule_html
            if "loadScheduleTimesByDate" in resp.url and resp.status == 200:
                try:
                    body = await resp.text()
                    data = decode(body)
                    if isinstance(data, dict):
                        html = data.get("strScheduleTimeData") or ""
                        if len(html) > 100:
                            schedule_html = html
                except Exception:
                    pass

        page.on("response", on_resp)
        url = f"{BASE}/movie/cinemaDetail.aspx?id={cinema['id']}&title={cinema['slug']}"
        print(f"  Playwright buka: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(6000)
        except Exception as e:
            print(f"  goto warning: {str(e)[:100]}")

        cookies = await ctx.cookies()
        for c in cookies:
            if c["name"] == "ASP.NET_SessionId":
                session_cookie = f"ASP.NET_SessionId={c['value']}"
                break

        await page.close()
        await browser.close()

    return schedule_html, session_cookie


async def main():
    print("=" * 60)
    print("PROBE: Apakah pCinemaId dipakai server?")
    print("=" * 60)

    # ─── Test 1: Playwright buka Cinema A, lalu httpx untuk A, B, C ───
    print(f"\n[Test 1] Playwright buka {CINEMA_A['name']}, httpx untuk 3 cinema berbeda")
    print("-" * 50)

    html_a_pw, session = await get_schedule_playwright_intercept(CINEMA_A)
    print(f"  Session dari Playwright (Cinema A): {session}")
    titles_a_pw = extract_titles(html_a_pw)
    print(f"  Film dari Playwright (Cinema A): {titles_a_pw}")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Cinema A via httpx (dengan session dari Playwright buka A)
        _, result_a = await get_schedule_httpx(client, CINEMA_A, session)
        print(f"\n  httpx Cinema A ({CINEMA_A['name']}):")
        print(f"    status={result_a.get('status')} len={result_a.get('len')}")
        print(f"    films={result_a.get('titles')}")

        # Cinema B via httpx (session masih dari Playwright buka A)
        _, result_b = await get_schedule_httpx(client, CINEMA_B, session)
        print(f"\n  httpx Cinema B ({CINEMA_B['name']}):")
        print(f"    status={result_b.get('status')} len={result_b.get('len')}")
        print(f"    films={result_b.get('titles')}")

        # Cinema C via httpx
        _, result_c = await get_schedule_httpx(client, CINEMA_C, session)
        print(f"\n  httpx Cinema C ({CINEMA_C['name']}):")
        print(f"    status={result_c.get('status')} len={result_c.get('len')}")
        print(f"    films={result_c.get('titles')}")

    # ─── Analisis ───
    print("\n" + "=" * 60)
    print("ANALISIS")
    print("=" * 60)
    all_same = (
        result_a.get("titles") == result_b.get("titles") == result_c.get("titles")
    )
    a_b_same = result_a.get("titles") == result_b.get("titles")
    a_c_same = result_a.get("titles") == result_c.get("titles")

    print(f"  A == B: {a_b_same}")
    print(f"  A == C: {a_c_same}")
    print(f"  Semua sama: {all_same}")

    if all_same:
        print("\n  ⚠ KESIMPULAN: Server MENGABAIKAN pCinemaId!")
        print("  Server pakai ASP.NET Session untuk tahu cinema mana.")
        print("  Setiap cinema butuh session SENDIRI (Playwright per cinema).")
        print()
        print("  SOLUSI: Untuk setiap cinema, buka Playwright baru → intercept")
        print("  loadScheduleTimesByDate → ambil HTML dari intercept (bukan POST ulang).")
    else:
        print("\n  ✓ KESIMPULAN: pCinemaId DIPAKAI server!")
        print("  Film berbeda per cinema → httpx dengan session dari satu Playwright OK.")
        print("  Masalah ada di tempat lain.")

    # ─── Test 2: Playwright buka Cinema B, bandingkan dengan A ───
    print("\n" + "=" * 60)
    print(f"[Test 2] Playwright buka {CINEMA_B['name']} langsung")
    print("-" * 50)
    html_b_pw, session_b = await get_schedule_playwright_intercept(CINEMA_B)
    titles_b_pw = extract_titles(html_b_pw)
    print(f"  Session dari Playwright (Cinema B): {session_b}")
    print(f"  Film dari Playwright (Cinema B): {titles_b_pw}")

    a_b_pw_same = titles_a_pw == titles_b_pw
    print(f"\n  Film Cinema A (PW) == Film Cinema B (PW): {a_b_pw_same}")
    if a_b_pw_same:
        print("  ⚠ Bahkan Playwright per cinema pun dapat film yang SAMA!")
        print("  → Server memang hanya punya 6 film yang tayang hari ini (semua cinema sama)")
        print("  → BUKAN bug scraper, tapi memang data Cinepolis hari ini sama untuk semua cinema")
    else:
        print("  ✓ Playwright per cinema dapat film BERBEDA → session per cinema diperlukan")

    # Simpan hasil
    out = {
        "cinema_a_playwright": {"name": CINEMA_A["name"], "titles": titles_a_pw},
        "cinema_b_playwright": {"name": CINEMA_B["name"], "titles": titles_b_pw},
        "cinema_a_httpx":      {"name": CINEMA_A["name"], **result_a},
        "cinema_b_httpx":      {"name": CINEMA_B["name"], **result_b},
        "cinema_c_httpx":      {"name": CINEMA_C["name"], **result_c},
        "all_same":            all_same,
        "playwright_same":     a_b_pw_same,
    }
    with open(OUT_DIR / "probe_schedule_per_cinema.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  Hasil disimpan: {OUT_DIR}/probe_schedule_per_cinema.json")


if __name__ == "__main__":
    asyncio.run(main())