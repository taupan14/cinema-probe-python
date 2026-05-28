"""
probe_cinepolis_v2.py
======================
Probe dengan error handling lebih baik + deteksi anti-bot.
Semua exception di-print detail untuk diagnosa.
"""
from __future__ import annotations

import asyncio
import json
import re
import ssl
import traceback
from datetime import date
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

BASE    = "https://cinepolis.co.id"
OUT_DIR = Path("probe_output")
OUT_DIR.mkdir(exist_ok=True)

today = date.today().strftime("%Y-%m-%d")


def save(name: str, data):
    path = OUT_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"    → saved: {path}")


def save_raw(name: str, text: str):
    path = OUT_DIR / f"{name}.txt"
    path.write_text(text, encoding="utf-8", errors="replace")
    print(f"    → saved: {path}")


def decode(text: str):
    try:
        outer = json.loads(text)
        d = outer.get("d", "{}")
        inner = json.loads(d) if isinstance(d, str) else d
        return inner.get("DataObject")
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PROBE A: Diagnosa koneksi dasar
# ─────────────────────────────────────────────────────────────────────────────

async def probe_a_connectivity():
    """Test koneksi paling dasar ke cinepolis.co.id."""
    print("\n" + "="*60)
    print("PROBE A: Koneksi dasar (GET /)")
    print("="*60)

    # Test 1: httpx biasa
    for ua, label in [
        ("python-httpx/0.27", "httpx_default_ua"),
        ("curl/7.88.1", "curl_ua"),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36",
            "chrome_ua"
        ),
    ]:
        print(f"\n  [{label}] GET {BASE}/")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(20.0),
                verify=True,
            ) as client:
                r = await client.get(BASE + "/", headers={"User-Agent": ua})
                print(f"    status={r.status_code}")
                print(f"    content-type={r.headers.get('content-type','?')}")
                print(f"    server={r.headers.get('server','?')}")
                print(f"    cf-ray={r.headers.get('cf-ray','(tidak ada)')}")
                print(f"    cf-cache={r.headers.get('cf-cache-status','(tidak ada)')}")
                print(f"    x-powered-by={r.headers.get('x-powered-by','(tidak ada)')}")
                print(f"    body_len={len(r.text)}")
                print(f"    body_preview={r.text[:300]!r}")
                save_raw(f"probe_a_{label}_body", r.text)
                save(f"probe_a_{label}_headers", dict(r.headers))
        except httpx.ConnectError as e:
            print(f"    ConnectError: {e}")
            print(f"    → Kemungkinan: DNS gagal / IP diblokir / firewall")
        except httpx.ConnectTimeout as e:
            print(f"    ConnectTimeout: {e}")
            print(f"    → Server tidak merespons dalam 20 detik")
        except httpx.SSLError as e:
            print(f"    SSLError: {e}")
            print(f"    → Masalah SSL/TLS certificate")
        except httpx.RemoteProtocolError as e:
            print(f"    RemoteProtocolError: {e}")
        except Exception as e:
            print(f"    {type(e).__name__}: {e}")
            traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# PROBE B: Playwright GET / dengan detail intercept
# ─────────────────────────────────────────────────────────────────────────────

async def probe_b_playwright_root():
    """Playwright GET / — lihat apakah ada Cloudflare challenge."""
    print("\n" + "="*60)
    print("PROBE B: Playwright GET / (deteksi Cloudflare/anti-bot)")
    print("="*60)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  Playwright tidak terinstall, skip")
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="id-ID",
            )
            page = await ctx.new_page()

            responses_log = []

            async def on_resp(r):
                responses_log.append({
                    "url": r.url,
                    "status": r.status,
                    "headers": dict(r.headers),
                })

            page.on("response", on_resp)

            print("  Membuka GET / ...")
            try:
                await page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                print(f"  goto warning: {type(e).__name__}: {str(e)[:200]}")

            # Tunggu sebentar untuk JS challenge
            await page.wait_for_timeout(4000)

            title = await page.title()
            url_now = page.url
            try:
                html = await page.content()
            except Exception:
                html = ""

            print(f"  title={title!r}")
            print(f"  final_url={url_now!r}")
            print(f"  html_len={len(html)}")

            # Deteksi Cloudflare
            is_cf = any(
                kw in html.lower() for kw in
                ["cloudflare", "cf-ray", "just a moment", "checking your browser",
                 "enable javascript", "ddos-guard", "__cf_bm"]
            )
            print(f"  cloudflare_detected={is_cf}")
            if is_cf:
                print("  ⚠ Cloudflare/bot-protection terdeteksi!")

            # Deteksi ASP.NET
            is_aspnet = any(
                kw in html for kw in ["__VIEWSTATE", "asp.net", "ASP.NET"]
            )
            print(f"  aspnet_detected={is_aspnet}")

            save_raw("probe_b_root_html", html)
            save("probe_b_responses_log", responses_log[:20])

            # Cek cookies yang di-set
            cookies = await ctx.cookies()
            print(f"\n  Cookies setelah load ({len(cookies)} total):")
            for c in cookies:
                print(f"    {c['name']}={c['value'][:40]!r}  domain={c['domain']}")

            save("probe_b_cookies", cookies)

            await page.close()
            await browser.close()

        return cookies

    except Exception as e:
        print(f"  EXCEPTION: {traceback.format_exc()}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# PROBE C: Playwright lengkap — cinema-page.aspx dengan wait yang benar
# ─────────────────────────────────────────────────────────────────────────────

async def probe_c_playwright_cinema_page():
    """
    Buka cinema-page.aspx dengan Playwright.
    Gunakan wait_until='domcontentloaded' (bukan 'networkidle') + explicit wait.
    Tangkap SEMUA network request/response.
    """
    print("\n" + "="*60)
    print("PROBE C: Playwright /cinema-page.aspx (wait=domcontentloaded)")
    print("="*60)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  Playwright tidak terinstall, skip")
        return None, None

    cinema_samples = []
    all_reqs = []
    all_resps = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="id-ID",
            )
            page = await ctx.new_page()

            async def on_request(req):
                all_reqs.append({
                    "url":       req.url,
                    "method":    req.method,
                    "post_data": req.post_data,
                })

            async def on_response(resp):
                try:
                    body = await resp.body()
                    body_text = body.decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
                all_resps.append({
                    "url":     resp.url,
                    "status":  resp.status,
                    "headers": dict(resp.headers),
                    "body_len": len(body_text),
                    "body_preview": body_text[:600],
                })

            page.on("request",  on_request)
            page.on("response", on_response)

            print("  Membuka /cinema-page.aspx ...")
            try:
                await page.goto(
                    f"{BASE}/cinema-page.aspx",
                    wait_until="domcontentloaded",
                    timeout=40_000,
                )
            except Exception as e:
                print(f"  goto: {type(e).__name__}: {str(e)[:200]}")

            # Tunggu 6 detik untuk AJAX selesai
            print("  Menunggu 6 detik untuk AJAX ...")
            await page.wait_for_timeout(6000)

            # Ambil HTML
            try:
                html = await page.content()
            except Exception as e:
                print(f"  page.content() error: {e}")
                html = ""

            print(f"  html_len={len(html)}")
            save_raw("probe_c_cinema_page", html)

            # Parse SelCinema links
            soup = BeautifulSoup(html, "lxml")
            links = soup.select("a[onclick*='SelCinema']")
            print(f"  SelCinema links: {len(links)}")
            for a in links[:5]:
                m = re.search(
                    r"SelCinema\(['\"]([^'\"]+)['\"],\s*['\"]([^'\"]+)['\"]",
                    a.get("onclick", "")
                )
                if m:
                    cinema_samples.append({
                        "id":   m.group(1),
                        "slug": m.group(2),
                        "text": a.get_text().strip(),
                    })
                    print(f"    id={m.group(1)!r}  slug={m.group(2)!r}  text={a.get_text().strip()!r}")

            # Cek struktur kota
            print("\n  Mencari struktur kota di HTML ...")
            for sel in ["[data-city]", "[data-kota]", ".city-name", ".city_name",
                        "h3", "h4", ".accordion-button"]:
                els = soup.select(sel)
                if els:
                    print(f"    selector={sel!r}  count={len(els)}  sample={els[0].get_text().strip()[:50]!r}")

            # Cookies
            cookies = await ctx.cookies()
            print(f"\n  Cookies ({len(cookies)}):")
            for c in cookies:
                print(f"    {c['name']}={c['value'][:50]!r}")

            await page.close()
            await browser.close()

        # Print semua network requests
        print(f"\n  Network requests ({len(all_reqs)} total):")
        for r in all_reqs:
            print(f"    {r['method']} {r['url'][:120]}")
            if r.get("post_data"):
                print(f"           POST_DATA: {r['post_data'][:200]}")

        print(f"\n  Network responses ({len(all_resps)} total):")
        for r in all_resps:
            has_data = r["body_len"] > 100
            marker = "✓" if has_data else "·"
            print(f"  {marker} {r['status']} {r['url'][:120]}")
            if has_data and ".aspx/" in r["url"]:
                print(f"           body_preview: {r['body_preview'][:200]!r}")

        save("probe_c_requests",  all_reqs)
        save("probe_c_responses", all_resps)
        save("probe_c_cinema_samples", cinema_samples)

        return cinema_samples, cookies

    except Exception as e:
        print(f"  EXCEPTION: {traceback.format_exc()}")
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# PROBE D: httpx dengan cookies dari Playwright
# ─────────────────────────────────────────────────────────────────────────────

async def probe_d_httpx_with_cookies(cookies: list):
    """
    Setelah dapat cookies dari Playwright (termasuk Cloudflare cookie __cf_bm),
    coba POST endpoint dengan httpx + cookies tersebut.
    """
    if not cookies:
        print("\n  PROBE D: skip (tidak ada cookies)")
        return

    print("\n" + "="*60)
    print("PROBE D: POST endpoints via httpx + cookies dari Playwright")
    print("="*60)

    # Build cookie jar dari Playwright cookies
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    print(f"  Cookie header: {cookie_header[:200]!r}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Content-Type":     "application/json; charset=utf-8",
        "Accept":           "application/json, text/javascript, */*; q=0.01",
        "Accept-Language":  "id-ID,id;q=0.9,en;q=0.8",
        "Origin":           BASE,
        "Referer":          f"{BASE}/cinema-page.aspx",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie":           cookie_header,
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        for url, payload, label in [
            (f"{BASE}/helper/CommonPageHelper.aspx/loadCities", {}, "loadCities"),
            (f"{BASE}/helper/CommonPageHelper.aspx/loadCinemas", {"cityId": ""}, "loadCinemas_empty"),
        ]:
            print(f"\n  POST {url.split('/')[-1]}  payload={payload}")
            try:
                r = await client.post(url, json=payload, headers=headers)
                print(f"    status={r.status_code}")
                print(f"    len={len(r.text)}")
                print(f"    preview={r.text[:300]!r}")
                if r.status_code == 200 and len(r.text) > 50:
                    dec = decode(r.text)
                    print(f"    decoded={str(dec)[:300]!r}")
                    save_raw(f"probe_d_{label}", r.text)
            except Exception as e:
                print(f"    {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# PROBE E: Intercept request dari cinemaDetail.aspx via Playwright
# ─────────────────────────────────────────────────────────────────────────────

async def probe_e_cinema_detail_playwright(cinema_id: str, cinema_slug: str):
    """
    Buka cinemaDetail.aspx via Playwright dan tangkap PERSIS semua AJAX.
    Ini yang paling penting untuk tahu payload loadScheduleTimesByDate.
    """
    print("\n" + "="*60)
    print(f"PROBE E: Playwright /movie/cinemaDetail.aspx")
    print(f"  cinema_id={cinema_id!r}  slug={cinema_slug!r}")
    print("="*60)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  Playwright tidak terinstall, skip")
        return

    detail_url = f"{BASE}/movie/cinemaDetail.aspx?id={cinema_id}&title={cinema_slug}"
    all_reqs  = []
    all_resps = []
    schedule_html = ""

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="id-ID",
            )
            page = await ctx.new_page()

            async def on_request(req):
                post_data = req.post_data or ""
                all_reqs.append({
                    "url":       req.url,
                    "method":    req.method,
                    "post_data": post_data,
                    "headers":   {k: v for k, v in req.headers.items()
                                  if k.lower() in (
                                      "content-type", "x-requested-with",
                                      "referer", "origin", "cookie",
                                      "__requestverificationtoken",
                                  )},
                })

            async def on_response(resp):
                nonlocal schedule_html
                try:
                    body = await resp.body()
                    body_text = body.decode("utf-8", errors="replace")
                except Exception:
                    body_text = ""
                entry = {
                    "url":          resp.url,
                    "status":       resp.status,
                    "body_len":     len(body_text),
                    "body_preview": body_text[:800],
                }
                all_resps.append(entry)
                if "loadScheduleTimesByDate" in resp.url and len(body_text) > 100:
                    schedule_html = body_text
                    save_raw("probe_e_schedule_raw", body_text)

            page.on("request",  on_request)
            page.on("response", on_response)

            print(f"  Membuka {detail_url} ...")
            try:
                await page.goto(detail_url, wait_until="domcontentloaded", timeout=40_000)
            except Exception as e:
                print(f"  goto: {type(e).__name__}: {str(e)[:200]}")

            print("  Menunggu 8 detik ...")
            await page.wait_for_timeout(8000)

            try:
                html = await page.content()
                save_raw("probe_e_cinema_detail_html", html)
            except Exception:
                html = ""

            cookies = await ctx.cookies()
            await page.close()
            await browser.close()

        # Print semua POST requests ke .aspx endpoints
        print(f"\n  Semua requests ({len(all_reqs)} total):")
        for r in all_reqs:
            if ".aspx" in r["url"] or "cinepolis" in r["url"]:
                print(f"    {r['method']} {r['url'][:100]}")
                if r.get("post_data"):
                    print(f"           POST: {r['post_data'][:300]}")
                if r.get("headers"):
                    for k, v in r["headers"].items():
                        print(f"           HDR {k}: {v[:80]}")

        print(f"\n  Semua responses ({len(all_resps)} total):")
        for r in all_resps:
            if r["body_len"] > 50:
                print(f"    {r['status']} {r['url'][:100]}")
                if ".aspx/" in r["url"]:
                    print(f"           preview: {r['body_preview'][:250]!r}")

        save("probe_e_requests",  all_reqs)
        save("probe_e_responses", all_resps)
        save("probe_e_cookies",   cookies)

        # Decode schedule
        if schedule_html:
            dec = decode(schedule_html)
            print(f"\n  loadScheduleTimesByDate decoded:")
            if isinstance(dec, dict):
                for k, v in dec.items():
                    vlen = len(str(v))
                    print(f"    key={k!r}  len={vlen}")
                    if k == "strScheduleTimeData":
                        soup = BeautifulSoup(str(v), "lxml")
                        movies = soup.select("div[id^='DivMovie_']")
                        print(f"    → {len(movies)} DivMovie_ blocks")
                        for mv in movies[:3]:
                            head = mv.select_one("div.head, h2, h3")
                            print(f"      film: {head.get_text().strip()[:60] if head else '?'!r}")
            save("probe_e_schedule_decoded", dec)
        else:
            print("\n  ⚠ loadScheduleTimesByDate TIDAK tertangkap!")
            print("    Kemungkinan: schedule tidak di-trigger otomatis,")
            print("    atau ada selector/button yang harus di-click dulu.")

            # Cek apakah ada date-picker / tab yang perlu di-click
            soup = BeautifulSoup(html, "lxml")
            print("\n  Elemen interaktif di halaman:")
            for sel in [
                "button", "a[onclick]", ".date-tab", ".nav-item",
                "[data-date]", "[onclick*='Schedule']", "[onclick*='Date']"
            ]:
                els = soup.select(sel)
                if els:
                    for el in els[:3]:
                        text = el.get_text().strip()[:50]
                        onclick = el.get("onclick", "")[:80]
                        print(f"    {sel}: text={text!r}  onclick={onclick!r}")

    except Exception as e:
        print(f"  EXCEPTION: {traceback.format_exc()}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    print("="*60)
    print("CINEPOLIS PROBE v2")
    print(f"Output: {OUT_DIR.absolute()}")
    print("="*60)

    # A: Koneksi dasar
    await probe_a_connectivity()

    # B: Playwright GET / — deteksi anti-bot
    cookies = await probe_b_playwright_root()

    # C: Playwright cinema-page.aspx
    cinema_samples, pw_cookies = await probe_c_playwright_cinema_page()
    if pw_cookies and not cookies:
        cookies = pw_cookies

    # D: httpx dengan cookies dari Playwright
    if cookies:
        await probe_d_httpx_with_cookies(cookies)

    # E: Playwright cinemaDetail.aspx
    if cinema_samples:
        s = cinema_samples[0]
        await probe_e_cinema_detail_playwright(s["id"], s["slug"])
    else:
        print("\n⚠ Tidak ada cinema_id untuk Probe E, skip")

    print("\n" + "="*60)
    print(f"Selesai. Cek folder: {OUT_DIR.absolute()}")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())