"""
diagnose_21cineplex.py
======================
Jalankan script ini DULU sebelum nulis scraper.
Tujuannya: lihat apa yang sebenarnya ada di halaman 21cineplex.

Usage:
    python diagnose_21cineplex.py

Output:
    - diagnose_output/home_rendered.html   → HTML setelah JS dijalankan
    - diagnose_output/mobile_rendered.html → HTML mobile site
    - diagnose_output/theater_page.html    → Halaman daftar theater
    - Log di terminal: class-class penting, link theater, network requests
"""

import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright, Request

OUTPUT_DIR = Path("diagnose_output")
OUTPUT_DIR.mkdir(exist_ok=True)

DESKTOP_URL = "https://21cineplex.com"
MOBILE_URL  = "https://m.21cineplex.com"

# Theater contoh untuk tes scraping movie (Bandung)
TEST_THEATER_URL = "https://m.21cineplex.com/gui.schedule.php?cinema_id=2&find_by=1"


async def diagnose():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ── 1. Intercept Network Requests ─────────────────────────────────────
        print("\n" + "="*60)
        print("[ LANGKAH 1 ] Intercept semua network request dari homepage")
        print("="*60)

        api_calls: list[dict] = []

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 11; Redmi Note 10) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            viewport={"width": 390, "height": 844},
        )
        page = await context.new_page()

        def on_request(req: Request):
            url = req.url
            # Filter: hanya tampilkan XHR/fetch/json requests (bukan gambar/css/font)
            if req.resource_type in ("xhr", "fetch"):
                api_calls.append({
                    "method": req.method,
                    "url": url,
                    "type": req.resource_type,
                })

        page.on("request", on_request)

        try:
            await page.goto(DESKTOP_URL, wait_until="networkidle", timeout=30000)
        except Exception as e:
            print(f"  [!] goto desktop timeout/error: {e} — lanjut...")

        print(f"\n  → {len(api_calls)} XHR/fetch requests ditemukan:\n")
        for r in api_calls:
            print(f"     [{r['method']}] {r['url']}")

        if not api_calls:
            print("  [!] Tidak ada XHR — site mungkin SSR atau blokir headless")

        # Simpan daftar API calls
        (OUTPUT_DIR / "api_calls.json").write_text(
            json.dumps(api_calls, indent=2), encoding="utf-8"
        )
        print(f"\n  → Disimpan ke: {OUTPUT_DIR}/api_calls.json")

        await context.close()

        # ── 2. Render Desktop Homepage ─────────────────────────────────────────
        print("\n" + "="*60)
        print("[ LANGKAH 2 ] Render desktop homepage, simpan HTML")
        print("="*60)

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        try:
            await page.goto(DESKTOP_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)  # tunggu lazy-load
        except Exception as e:
            print(f"  [!] {e}")

        html = await page.content()
        (OUTPUT_DIR / "home_rendered.html").write_text(html, encoding="utf-8")
        print(f"  → Disimpan: {OUTPUT_DIR}/home_rendered.html ({len(html)} chars)")

        # Inspect: cari link yang mengandung kata theater/bioskop
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: e.innerText.trim(), href: e.href})).filter(x => x.text)"
        )
        theater_links = [l for l in links if any(
            k in l["href"].lower() for k in ["theater", "bioskop", "cinema", "schedule"]
        )]
        print(f"\n  → Link theater/bioskop ditemukan: {len(theater_links)}")
        for l in theater_links[:20]:
            print(f"     {l['text'][:40]:40s}  →  {l['href']}")

        # Inspect: semua CSS classes unik
        classes = await page.evaluate("""
            () => {
                const cls = new Set();
                document.querySelectorAll('[class]').forEach(el => {
                    el.className.toString().split(/\\s+/).forEach(c => { if(c) cls.add(c); });
                });
                return [...cls].sort();
            }
        """)
        print(f"\n  → CSS classes unik: {len(classes)}")
        # Filter class yang relevan untuk scraping
        relevant = [c for c in classes if any(
            k in c.lower() for k in [
                "movie", "film", "theater", "cinema", "schedule",
                "title", "show", "card", "item", "list"
            ]
        )]
        print(f"  → Relevant classes: {relevant}")

        await context.close()

        # ── 3. Render Mobile Site ──────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ LANGKAH 3 ] Render mobile site")
        print("="*60)

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 12; Samsung Galaxy S21) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            viewport={"width": 390, "height": 844},
        )
        page = await context.new_page()
        try:
            await page.goto(MOBILE_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  [!] {e}")

        html = await page.content()
        (OUTPUT_DIR / "mobile_rendered.html").write_text(html, encoding="utf-8")
        print(f"  → Disimpan: {OUTPUT_DIR}/mobile_rendered.html ({len(html)} chars)")

        # Cek apakah ada dropdown select theater
        selects = await page.eval_on_selector_all(
            "select",
            "els => els.map(e => ({ id: e.id, name: e.name, optionCount: e.options.length, firstOptions: [...e.options].slice(0,5).map(o => ({val: o.value, text: o.text})) }))"
        )
        print(f"\n  → <select> elements ditemukan: {len(selects)}")
        for s in selects:
            print(f"     id='{s['id']}' name='{s['name']}' options={s['optionCount']}")
            for o in s.get("firstOptions", []):
                print(f"       val={o['val']!r:15s} text={o['text']!r}")

        await context.close()

        # ── 4. Render Mobile Schedule untuk 1 Theater ─────────────────────────
        print("\n" + "="*60)
        print(f"[ LANGKAH 4 ] Render mobile schedule: {TEST_THEATER_URL}")
        print("="*60)

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            viewport={"width": 390, "height": 844},
        )
        page = await context.new_page()
        try:
            await page.goto(TEST_THEATER_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  [!] {e}")

        html = await page.content()
        (OUTPUT_DIR / "mobile_schedule.html").write_text(html, encoding="utf-8")
        print(f"  → Disimpan: {OUTPUT_DIR}/mobile_schedule.html ({len(html)} chars)")

        # Cari semua teks yang mungkin judul film
        texts = await page.evaluate("""
            () => {
                const tags = ['h1','h2','h3','h4','h5','strong','b','.title','.movie-title'];
                const results = [];
                document.querySelectorAll(tags.join(',')).forEach(el => {
                    const t = el.innerText.trim();
                    if (t && t.length > 2 && t.length < 100) results.push(t);
                });
                return [...new Set(results)];
            }
        """)
        print(f"\n  → Teks dari heading/strong ({len(texts)} unique):")
        for t in texts[:30]:
            print(f"     {t!r}")

        # Struktur HTML: elemen pertama yang mungkin movie card
        structure = await page.evaluate("""
            () => {
                const candidates = document.querySelectorAll(
                    'div, li, article, section'
                );
                const results = [];
                for (const el of candidates) {
                    const cls = el.className.toString().trim();
                    const text = el.innerText.trim().slice(0, 80);
                    if (cls && text && text.length > 10) {
                        results.push({ tag: el.tagName, cls, text });
                    }
                    if (results.length >= 30) break;
                }
                return results;
            }
        """)
        print(f"\n  → Elemen HTML dengan class (30 pertama):")
        for el in structure:
            print(f"     <{el['tag'].lower()} class='{el['cls'][:50]}'> {el['text'][:60]!r}")

        await context.close()
        await browser.close()

        # ── 5. Ringkasan ───────────────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ SELESAI ] File output:")
        print("="*60)
        for f in sorted(OUTPUT_DIR.iterdir()):
            size = f.stat().st_size
            print(f"  {f.name:35s} {size:>8,} bytes")

        print("""
[ LANGKAH SELANJUTNYA ]
  1. Buka diagnose_output/mobile_schedule.html di browser → lihat struktur HTML asli
  2. Perhatikan output terminal di atas:
     - Ada XHR/API calls? → kita bisa hit langsung tanpa Playwright
     - Ada <select> dengan options? → kita bisa ambil daftar theater dari situ
     - Ada heading/class film? → kita update selector di scraper
  3. Share hasilnya jika butuh bantuan analisis lebih lanjut
""")


if __name__ == "__main__":
    asyncio.run(diagnose())