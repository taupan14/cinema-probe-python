"""
probe_cinema_ids.py
===================
Tujuan: Temukan cinema_id yang valid untuk dc21-api /cinema/schedule/theater
Caranya: Playwright buka halaman cinemas, intercept request yang dibuat app,
         extract cinema_id dari URL atau payload yang dikirim.

Jalankan: python probe_cinema_ids.py
Output:
  - probe_output/cinema_ids.json       → semua cinema_id + nama yang ditemukan
  - probe_output/schedule_sample.json  → contoh response schedule per theater
"""

import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright
import httpx

OUTPUT_DIR = Path("probe_output")
OUTPUT_DIR.mkdir(exist_ok=True)

MOBILE   = "https://m.21cineplex.com"
DC21_API = "https://dc21-api.21cineplex.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Referer": f"{MOBILE}/",
    "Origin":  MOBILE,
    "Accept":  "application/json, */*",
}


async def find_cinema_ids_via_playwright() -> list[dict]:
    """
    Buka halaman /cinemas di Playwright, intercept semua request,
    cari cinema_id dari URL atau POST body.
    """
    found: list[dict] = []
    seen_ids: set[str] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 390, "height": 844},
        )
        page = await context.new_page()

        # Intercept semua request
        async def on_request(req):
            url = req.url
            if "cinema" in url.lower() or "theater" in url.lower() or "schedule" in url.lower():
                print(f"  [REQ] {req.method} {url}")
                # Coba extract cinema_id dari URL
                m = re.search(r'cinema_id=([A-Z0-9]+)', url)
                if m:
                    cid = m.group(1)
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        found.append({"cinema_id": cid, "source": "url_param"})

        async def on_response(resp):
            url = resp.url
            if "schedule/theater" in url or "theater" in url.lower():
                try:
                    body = await resp.text()
                    if len(body) > 50:
                        print(f"  [RESP] {url} → {len(body)} bytes")
                        print(f"         {body[:300]!r}")
                        try:
                            data = json.loads(body)
                            # Cari cinema_id di response
                            if isinstance(data, dict):
                                val = data.get("data", {})
                                if isinstance(val, dict):
                                    theaters = val.get("value", [])
                                    if isinstance(theaters, list):
                                        for t in theaters:
                                            if isinstance(t, dict):
                                                cid = (
                                                    t.get("cinema_id") or
                                                    t.get("theater_id") or
                                                    t.get("id") or ""
                                                )
                                                name = (
                                                    t.get("name") or
                                                    t.get("theater_name") or ""
                                                )
                                                if cid and cid not in seen_ids:
                                                    seen_ids.add(str(cid))
                                                    found.append({
                                                        "cinema_id": cid,
                                                        "name": name,
                                                        "source": "response_body",
                                                    })
                        except Exception:
                            pass
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        # ── Langkah 1: Buka halaman /cinemas ──────────────────────────────
        print("\n[ A ] Buka /cinemas ...")
        try:
            await page.goto(f"{MOBILE}/cinemas", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  [!] {e}")

        # Dump HTML untuk inspeksi manual
        html = await page.content()
        (OUTPUT_DIR / "cinemas_page.html").write_text(html, encoding="utf-8")
        print(f"  → HTML disimpan ({len(html)} chars)")

        # ── Langkah 2: Cari dan klik semua kota/theater ───────────────────
        print("\n[ B ] Cari dropdown atau list kota ...")

        # Coba klik dropdown kota jika ada
        try:
            city_dropdowns = await page.query_selector_all("select, [role='combobox'], [class*='city'], [class*='kota']")
            print(f"  → {len(city_dropdowns)} potential city selectors")
            for el in city_dropdowns[:3]:
                tag = await el.evaluate("e => e.tagName")
                cls = await el.evaluate("e => e.className")
                print(f"     <{tag} class='{cls[:60]}'>")
        except Exception as e:
            print(f"  [!] {e}")

        # Coba extract semua link yang mengandung cinema/theater
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: e.innerText.trim()})).filter(x => x.text)"
        )
        cinema_links = [l for l in links if any(
            k in l["href"].lower() for k in ["cinema", "theater", "schedule"]
        )]
        print(f"\n[ C ] Cinema/theater links: {len(cinema_links)}")
        for l in cinema_links[:30]:
            print(f"     {l['text'][:40]:40s} → {l['href']}")
            # Extract cinema_id dari link
            m = re.search(r'/cinemas?/([A-Z0-9]{4,10})', l["href"])
            if m:
                cid = m.group(1)
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    found.append({
                        "cinema_id": cid,
                        "name": l["text"],
                        "source": "page_link",
                    })

        # ── Langkah 3: Klik beberapa theater untuk trigger network request ─
        print(f"\n[ D ] Klik theater links untuk trigger schedule request ...")
        for link in cinema_links[:5]:
            try:
                print(f"  → Navigasi ke: {link['href']}")
                await page.goto(link["href"], wait_until="networkidle", timeout=15000)
                await page.wait_for_timeout(2000)

                # Extract cinema_id dari URL saat ini
                current_url = page.url
                m = re.search(r'cinema_id=([A-Z0-9]+)', current_url)
                if m:
                    cid = m.group(1)
                    if cid not in seen_ids:
                        seen_ids.add(cid)
                        found.append({"cinema_id": cid, "name": link["text"], "source": "navigation"})

            except Exception as e:
                print(f"     [!] {e}")

        # ── Langkah 4: Coba POST /api/theater dengan Playwright cookies ───
        print(f"\n[ E ] Ambil cookies dari browser untuk re-use di httpx ...")
        cookies = await context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        print(f"  → {len(cookies)} cookies: {cookie_str[:200]!r}")

        await browser.close()

    return found, cookie_str


async def probe_schedule_with_ids(cinema_ids: list[str], cookie_str: str):
    """
    Test endpoint schedule/theater dengan cinema_id yang ditemukan.
    """
    print(f"\n[ F ] Test dc21-api dengan {len(cinema_ids)} cinema_id ...")
    headers = {**HEADERS, "Cookie": cookie_str}
    results = []

    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
        for cid in cinema_ids[:10]:  # max 10
            url = f"{DC21_API}/cinema/schedule/theater"
            try:
                r = await client.get(url, params={"cinema_id": cid})
                body_len = len(r.text)
                print(f"  cinema_id={cid}: status={r.status_code} len={body_len}")
                if r.status_code == 200:
                    try:
                        data = r.json()
                        value = data.get("data", {}).get("value", [])
                        print(f"    → value: {len(value) if isinstance(value, list) else value} items")
                        if isinstance(value, list) and len(value) > 0:
                            print(f"    🎉 DATA DITEMUKAN! Sample: {json.dumps(value[0], ensure_ascii=False)[:300]}")
                            results.append({"cinema_id": cid, "data": data})
                            path = OUTPUT_DIR / f"schedule_{cid}.json"
                            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                            print(f"    → Disimpan: {path}")
                    except Exception as e:
                        print(f"    [!] Parse error: {e}")
                        print(f"    body: {r.text[:200]!r}")
            except Exception as e:
                print(f"  cinema_id={cid}: ERROR {e}")
            await asyncio.sleep(0.5)

    return results


async def main():
    print("="*60)
    print("PHASE 1: Cari cinema_id valid via Playwright")
    print("="*60)
    found, cookie_str = await find_cinema_ids_via_playwright()

    print(f"\n→ Total cinema_id ditemukan: {len(found)}")
    for item in found:
        print(f"  {item}")

    if found:
        path = OUTPUT_DIR / "cinema_ids.json"
        path.write_text(json.dumps(found, indent=2, ensure_ascii=False))
        print(f"→ Disimpan: {path}")

    # Deduplikasi
    unique_ids = list({item["cinema_id"] for item in found})
    print(f"\n→ Unique cinema_id: {unique_ids}")

    if not unique_ids:
        print("\n[!] Tidak ada cinema_id ditemukan via Playwright.")
        print("    Coba manual: buka https://m.21cineplex.com/cinemas di browser,")
        print("    DevTools → Network → filter 'schedule' → lihat request URL/payload")
        return

    print("\n" + "="*60)
    print("PHASE 2: Test schedule endpoint dengan cinema_id yang ditemukan")
    print("="*60)
    results = await probe_schedule_with_ids(unique_ids, cookie_str)

    if results:
        print(f"\n✅ Berhasil! {len(results)} cinema dengan data schedule.")
        print("   Scraper bisa diupgrade ke mode per-theater.")
    else:
        print("\n⚠️  Semua cinema_id return value=[]. Kemungkinan:")
        print("   1. cinema_id format berbeda dari yang diharapkan API")
        print("   2. Perlu header/token tambahan (Authorization dll)")
        print("   3. API schedule/theater memang tidak publik")


if __name__ == "__main__":
    asyncio.run(main())