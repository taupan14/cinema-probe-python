"""
diagnose_cinepolis.py
=====================
Probe website cinepolis.co.id untuk temukan:
1. Apakah site JS-rendered atau SSR?
2. XHR/fetch API calls yang dibuat browser
3. Endpoint untuk kota, cinema, movies, showtimes
4. Struktur response JSON

Jalankan: python diagnose_cinepolis.py
Output:   diagnose_cinepolis/
"""
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path("diagnose_cinepolis")
OUTPUT_DIR.mkdir(exist_ok=True)

BASE_URL = "https://cinepolis.co.id"

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)


async def diagnose():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ── 1. Intercept semua XHR/fetch dari homepage ────────────────────────
        print("\n" + "="*60)
        print("[ 1 ] XHR/fetch intercept — homepage desktop")
        print("="*60)

        api_calls: list[dict] = []
        responses: list[dict] = []

        context = await browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        def on_request(req):
            if req.resource_type in ("xhr", "fetch"):
                api_calls.append({
                    "method": req.method,
                    "url":    req.url,
                    "type":   req.resource_type,
                })

        async def on_response(resp):
            if resp.request.resource_type in ("xhr", "fetch"):
                try:
                    body = await resp.text()
                    responses.append({
                        "url":    resp.url,
                        "status": resp.status,
                        "len":    len(body),
                        "ct":     resp.headers.get("content-type", ""),
                        "body":   body[:500],
                    })
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  [!] goto error: {e}")

        print(f"\n  → {len(api_calls)} XHR/fetch requests:\n")
        for r in api_calls:
            print(f"     [{r['method']}] {r['url']}")

        print(f"\n  → Responses dengan data (len > 50):")
        for r in responses:
            if r["len"] > 50:
                print(f"     {r['status']} {r['url']}")
                print(f"     len={r['len']} ct={r['ct'][:50]}")
                print(f"     body: {r['body'][:200]!r}")
                print()

        (OUTPUT_DIR / "api_calls.json").write_text(
            json.dumps(api_calls, indent=2), encoding="utf-8"
        )
        (OUTPUT_DIR / "responses.json").write_text(
            json.dumps(responses, indent=2), encoding="utf-8"
        )

        # Simpan HTML
        html = await page.content()
        (OUTPUT_DIR / "homepage_desktop.html").write_text(html, encoding="utf-8")
        print(f"  → HTML disimpan ({len(html)} chars)")

        # CSS classes relevan
        classes = await page.evaluate("""
            () => {
                const cls = new Set();
                document.querySelectorAll('[class]').forEach(el => {
                    el.className.toString().split(/\\s+/).forEach(c => { if(c) cls.add(c); });
                });
                return [...cls].sort();
            }
        """)
        relevant = [c for c in classes if any(
            k in c.lower() for k in [
                "movie", "film", "cinema", "theater", "schedule",
                "showtime", "city", "kota", "ticket", "now"
            ]
        )]
        print(f"  → Relevant CSS classes: {relevant[:30]}")

        # Links yang mengandung kata kunci cinema/movie
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: e.innerText.trim(), href: e.href})).filter(x => x.text && x.text.length < 80)"
        )
        cinema_links = [l for l in links if any(
            k in l["href"].lower() for k in [
                "cinema", "movie", "film", "schedule", "showtime",
                "bioskop", "jadwal", "now-showing"
            ]
        )]
        print(f"\n  → Cinema/movie links: {len(cinema_links)}")
        for l in cinema_links[:20]:
            print(f"     {l['text'][:40]:40s} → {l['href']}")

        await context.close()

        # ── 2. Cek halaman /now-showing atau /movies ──────────────────────────
        print("\n" + "="*60)
        print("[ 2 ] Probe halaman-halaman utama")
        print("="*60)

        test_pages = [
            "/",
            "/now-showing",
            "/movies",
            "/cinemas",
            "/cinema",
            "/schedules",
            "/schedule",
            "/showtimes",
        ]

        context2 = await browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1280, "height": 800},
        )

        import httpx
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": DESKTOP_UA},
        ) as client:
            for path in test_pages:
                url = f"{BASE_URL}{path}"
                try:
                    resp = await client.get(url)
                    print(f"  {path:25s} → {resp.status_code} len={len(resp.text)}")
                    if resp.status_code == 200 and len(resp.text) > 1000:
                        fname = path.strip("/").replace("/", "_") or "home"
                        (OUTPUT_DIR / f"page_{fname}.html").write_text(
                            resp.text, encoding="utf-8"
                        )
                except Exception as e:
                    print(f"  {path:25s} → ERROR: {e}")

        await context2.close()

        # ── 3. Probe API endpoints umum ───────────────────────────────────────
        print("\n" + "="*60)
        print("[ 3 ] Probe API endpoints")
        print("="*60)

        api_endpoints = [
            # REST API patterns
            "/api/cities",
            "/api/city",
            "/api/cinemas",
            "/api/cinema",
            "/api/movies",
            "/api/movie",
            "/api/now-showing",
            "/api/schedules",
            "/api/showtimes",
            # Cinepolis global patterns
            "/v1/cinemas",
            "/v1/movies",
            "/v1/cities",
            # Possible subdomains via path
            "/api/v1/cinemas",
            "/api/v1/movies",
            "/api/v1/cities",
        ]

        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent":  DESKTOP_UA,
                "Accept":      "application/json, */*",
                "Referer":     BASE_URL,
            },
        ) as client:
            for ep in api_endpoints:
                url = f"{BASE_URL}{ep}"
                try:
                    resp = await client.get(url)
                    ct = resp.headers.get("content-type", "")
                    print(f"  {ep:35s} → {resp.status_code} len={len(resp.text)} ct={ct[:30]}")
                    if resp.status_code == 200 and len(resp.text) > 50:
                        print(f"     body: {resp.text[:200]!r}")
                        (OUTPUT_DIR / f"api_{ep.strip('/').replace('/', '_')}.json").write_text(
                            resp.text, encoding="utf-8"
                        )
                except Exception as e:
                    print(f"  {ep:35s} → ERROR: {e}")

        # ── 4. Render halaman now-showing dengan Playwright ───────────────────
        print("\n" + "="*60)
        print("[ 4 ] Render now-showing page dengan Playwright")
        print("="*60)

        now_showing_urls = [
            f"{BASE_URL}/now-showing",
            f"{BASE_URL}/movies",
            f"{BASE_URL}/jadwal",
        ]

        context3 = await browser.new_context(
            user_agent=DESKTOP_UA,
            viewport={"width": 1280, "height": 800},
        )
        page3 = await context3.new_page()

        ns_api_calls = []
        async def on_ns_response(resp):
            if resp.request.resource_type in ("xhr", "fetch") and resp.status == 200:
                try:
                    body = await resp.text()
                    if len(body) > 100:
                        ns_api_calls.append({
                            "url":  resp.url,
                            "len":  len(body),
                            "body": body[:600],
                        })
                except Exception:
                    pass

        page3.on("response", on_ns_response)

        for url in now_showing_urls:
            ns_api_calls.clear()
            try:
                print(f"\n  Membuka: {url}")
                await page3.goto(url, wait_until="networkidle", timeout=20000)
                await page3.wait_for_timeout(2000)

                html3 = await page3.content()
                fname = url.replace(BASE_URL, "").strip("/").replace("/", "_") or "nowshowing"
                (OUTPUT_DIR / f"rendered_{fname}.html").write_text(html3, encoding="utf-8")
                print(f"  → HTML disimpan ({len(html3)} chars)")

                if ns_api_calls:
                    print(f"  → API calls ({len(ns_api_calls)}):")
                    for c in ns_api_calls:
                        print(f"     {c['url']}")
                        print(f"     len={c['len']} body: {c['body'][:200]!r}")
                        print()

                # Extract heading teks (judul film kandidat)
                texts = await page3.evaluate("""
                    () => {
                        const tags = ['h1','h2','h3','h4','.movie-title','.film-title','.title'];
                        const results = [];
                        document.querySelectorAll(tags.join(',')).forEach(el => {
                            const t = el.innerText.trim();
                            if (t && t.length > 1 && t.length < 100) results.push(t);
                        });
                        return [...new Set(results)].slice(0, 20);
                    }
                """)
                print(f"  → Heading texts: {texts}")

            except Exception as e:
                print(f"  [!] {e}")

        await context3.close()

        # ── 5. Ringkasan ───────────────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ SELESAI ] Output files:")
        print("="*60)
        for f in sorted(OUTPUT_DIR.iterdir()):
            print(f"  {f.name:45s} {f.stat().st_size:>8,} bytes")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(diagnose())