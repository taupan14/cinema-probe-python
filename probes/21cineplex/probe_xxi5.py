"""
probe_xxi5.py — Investigasi:
  1. Nama cinema dari mana (title page vs getTheaterSchedule vs getAllTheater)
  2. Alamat + koordinat dari mana
  3. Kenapa showtime tidak lengkap (cek Backrooms di Bandung)

Jalankan: python probe_xxi5.py 2>&1 | tee probe5_result.txt
"""
from __future__ import annotations
import asyncio, json, re

MOBILE_BASE = "https://m.21cineplex.com"
MOBILE_UA   = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)

# Semua BDG cinema_id dari sitemap
BDG_IDS = [
    "BDGBRAG", "BDGCIWL", "BDGBTC", "BDGEMPI", "BDGFECI",
    "BDGJATO", "BDGSUBA", "BDGTEAV", "BDGTHMA", "BDGTRBB",
    "BDGBSM",  "BDGUBER",
]


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=MOBILE_UA, viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()

        # ── ANALISA 1: Struktur lengkap dari /cinemas/{id} ────────────────
        print("\n" + "="*60)
        print("ANALISA 1 — Full response getTheaterSchedule untuk BDGTEAV")
        print("="*60)

        raw_responses: list[dict] = []
        all_api_calls: list[str]  = []

        async def cap_all(resp):
            all_api_calls.append(resp.url)
            if any(kw in resp.url for kw in ["theater", "movie", "schedule", "cinema", "api"]):
                try:
                    text = await resp.text()
                    if text.strip().startswith("{"):
                        raw_responses.append({"url": resp.url, "body": json.loads(text)})
                except Exception: pass

        page.on("response", cap_all)
        await page.goto(f"{MOBILE_BASE}/cinemas/BDGTEAV", wait_until="networkidle", timeout=20_000)
        await page.wait_for_timeout(3000)
        page.remove_listener("response", cap_all)

        print(f"\nSemua API calls ({len(all_api_calls)}):")
        for u in sorted(set(all_api_calls)):
            if "21cineplex" in u:
                print(f"  {u}")

        for item in raw_responses:
            url  = item["url"]
            body = item["body"]
            print(f"\n--- {url} ---")
            if "getTheaterSchedule" in url:
                print(f"Top keys: {list(body.keys())}")
                data = (body.get("data") or {})
                print(f"data keys: {list(data.keys())}")
                value = data.get("value") or []
                print(f"value length (days): {len(value)}")
                if value:
                    day0 = value[0]
                    print(f"day[0] keys: {list(day0.keys())}")
                    print(f"day[0].date: {day0.get('date')}")
                    print(f"day[0].timezone: {day0.get('timezone')}")
                    cinema_sec = day0.get("cinema") or {}
                    print(f"day[0].cinema subtypes: {list(cinema_sec.keys())}")
                    for stype, films in cinema_sec.items():
                        print(f"  [{stype}] {len(films)} films")
                        if films:
                            f0 = films[0]
                            print(f"    film[0] keys: {list(f0.keys())}")
                            print(f"    title: {f0.get('title')!r}")
                            print(f"    cafe_group: {f0.get('cafe_group')!r}")
                            sched = f0.get("schedule") or []
                            print(f"    schedule entries: {len(sched)}")
                            if sched:
                                print(f"    schedule[0]: {json.dumps(sched[0], indent=6)[:400]}")
            elif "getCityList" not in url and "analytics" not in url:
                print(json.dumps(body, indent=2)[:500])

        # ── ANALISA 2: Page content — nama, alamat, koordinat ─────────────
        print("\n" + "="*60)
        print("ANALISA 2 — HTML content page /cinemas/BDGTEAV")
        print("="*60)

        title = await page.title()
        print(f"Page title: {title!r}")

        # Cek meta tags
        meta_info = await page.evaluate("""() => {
            const metas = {};
            document.querySelectorAll('meta').forEach(m => {
                const name = m.getAttribute('name') || m.getAttribute('property') || '';
                const content = m.getAttribute('content') || '';
                if (name && content) metas[name] = content;
            });
            // Cek JSON-LD structured data
            const jsonlds = [];
            document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                try { jsonlds.push(JSON.parse(s.textContent)); } catch(e) {}
            });
            // Cek teks yang mungkin berisi alamat
            const h1 = document.querySelector('h1')?.innerText || '';
            const h2 = document.querySelector('h2')?.innerText || '';
            const address = document.querySelector('[class*="address"]')?.innerText || '';
            return { metas, jsonlds, h1, h2, address };
        }""")

        print(f"\nh1: {meta_info.get('h1')!r}")
        print(f"h2: {meta_info.get('h2')!r}")
        print(f"address element: {meta_info.get('address')!r}")
        print(f"\nMeta tags:")
        for k, v in (meta_info.get("metas") or {}).items():
            if any(kw in k.lower() for kw in ["title", "desc", "loc", "geo", "name", "og:"]):
                print(f"  {k}: {v!r}")
        print(f"\nJSON-LD structured data:")
        for ld in (meta_info.get("jsonlds") or []):
            print(f"  {json.dumps(ld, indent=4)[:600]}")

        # Cek Next.js __NEXT_DATA__
        next_data = await page.evaluate("""() => {
            const el = document.getElementById('__NEXT_DATA__');
            if (!el) return null;
            try { return JSON.parse(el.textContent); } catch(e) { return null; }
        }""")

        if next_data:
            print(f"\n__NEXT_DATA__ found! Keys: {list(next_data.keys())}")
            props = next_data.get("props") or {}
            page_props = props.get("pageProps") or {}
            print(f"pageProps keys: {list(page_props.keys())}")
            # Cari data cinema
            for k, v in page_props.items():
                if isinstance(v, dict):
                    print(f"  pageProps.{k} keys: {list(v.keys())[:10]}")
                elif isinstance(v, list):
                    print(f"  pageProps.{k}: list[{len(v)}]")
                else:
                    print(f"  pageProps.{k}: {str(v)[:100]!r}")
        else:
            print("\n__NEXT_DATA__ tidak ditemukan")

        # ── ANALISA 3: Semua BDG cinema — nama dan film Backrooms ─────────
        print("\n" + "="*60)
        print("ANALISA 3 — Cek 12 cinema BDG: nama + apakah ada Backrooms")
        print("="*60)

        for cid in BDG_IDS:
            sched_data: list[dict] = []

            async def cap_sched(resp, sd=sched_data):
                if "getTheaterSchedule" in resp.url:
                    try:
                        body  = json.loads(await resp.text())
                        value = (body.get("data") or {}).get("value") or []
                        if isinstance(value, list): sd.extend(value)
                    except: pass

            page.on("response", cap_sched)
            r = await page.goto(
                f"{MOBILE_BASE}/cinemas/{cid}",
                wait_until="networkidle", timeout=15_000
            )
            await page.wait_for_timeout(2000)
            page.remove_listener("response", cap_sched)

            title_page = await page.title()
            cinema_name = title_page.split(" | ")[0].strip() if " | " in title_page else title_page

            # Cek Next.js data untuk alamat + koordinat
            next_d = await page.evaluate("""() => {
                const el = document.getElementById('__NEXT_DATA__');
                if (!el) return null;
                try { return JSON.parse(el.textContent); } catch(e) { return null; }
            }""")

            address = ""
            lat, lng = 0.0, 0.0
            if next_d:
                pp = (next_d.get("props") or {}).get("pageProps") or {}
                # Cari field alamat dan koordinat di pageProps
                def find_fields(obj, depth=0):
                    nonlocal address, lat, lng
                    if depth > 5 or not isinstance(obj, (dict, list)):
                        return
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            kl = k.lower()
                            if "address" in kl and isinstance(v, str) and v:
                                address = v
                            if kl in ("lat", "latitude") and isinstance(v, (int, float)) and v:
                                lat = float(v)
                            if kl in ("lng", "lon", "longitude") and isinstance(v, (int, float)) and v:
                                lng = float(v)
                            if kl == "coordinate" and isinstance(v, str) and "," in v:
                                parts = v.split(",")
                                try:
                                    lat = float(parts[0].strip())
                                    lng = float(parts[1].strip())
                                except: pass
                            find_fields(v, depth+1)
                    elif isinstance(obj, list):
                        for item in obj[:5]:
                            find_fields(item, depth+1)
                find_fields(pp)

            # Cari Backrooms di schedule
            has_backrooms = False
            total_films   = 0
            for day in sched_data:
                for stype, films in (day.get("cinema") or {}).items():
                    if isinstance(films, list):
                        total_films += len(films)
                        for f in films:
                            title_film = (f.get("title") or "").lower()
                            if "backroom" in title_film:
                                has_backrooms = True

            br_mark = "🎬 BACKROOMS" if has_backrooms else ""
            print(f"  {cid} → {cinema_name!r:40s} addr={address[:40]!r} lat={lat} lng={lng} films={total_films} {br_mark}")

        # ── ANALISA 4: getTheaterSchedule response via direct fetch ────────
        print("\n" + "="*60)
        print("ANALISA 4 — Direct fetch getTheaterSchedule via JS (dengan session)")
        print("="*60)

        # Test apakah bisa fetch langsung dengan POST body cinema_id
        for cid in ["BDGTEAV", "BDGBRAG", "BDGUBER"]:
            result = await page.evaluate(f"""async () => {{
                try {{
                    const r = await fetch('/api/theater?type=getTheaterSchedule', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json', 'Accept': 'application/json'}},
                        body: JSON.stringify({{cinema_id: '{cid}'}})
                    }});
                    const data = await r.json();
                    const val = (data?.data?.value) || [];
                    const films = val[0]?.cinema?.xxi || [];
                    return {{
                        status: r.status,
                        days: val.length,
                        films_day0: films.length,
                        sample_title: films[0]?.title || ''
                    }};
                }} catch(e) {{ return {{error: e.message}}; }}
            }}""")
            print(f"  POST getTheaterSchedule cinema_id={cid}: {result}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())