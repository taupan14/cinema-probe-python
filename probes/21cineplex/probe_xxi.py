"""
probe_xxi4.py — Parse semua cinema_id dari sitemap.xml dan test getTheaterSchedule

Jalankan: python probe_xxi4.py 2>&1 | tee probe4_result.txt
"""
from __future__ import annotations
import asyncio, json, re
from collections import defaultdict

MOBILE_BASE = "https://m.21cineplex.com"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)

KNOWN_JABODETABEK = {
    "BGRAESC","BGRBOSQ","BGRBOTA","BGRBOXI","BGRBTM","BGRCIBE","BGRCICI",
    "BGRCICM","BGRCINE","BGRCIPA","BGRDEPK","BGRLWKW","BGRMACI","BGRMECI",
    "BGRPESQ","BGRPLJD","BGRQSQU","BGRRATA","BGRTPSA","BGRTRBO","BGRTSCI",
    "BKSBESQ","BKSCICB","BKSCICI","BKSCOKH","BKSGRKB","BKSGRME","BKSGRWA",
    "BKSLWGW","BKSMEBE","BKSMETL","BKSPABE","BKSPLCI","BKSPOGD","BKSSUBE",
    "BKSTRJU","JKTAETB","JKTAGT9","JKTANGG","JKTARGA","JKTARIO","JKTATRI",
    "JKTBAPL","JKTBASS","JKTBLOM","JKTBMSQ","JKTCICE","JKTCIJA","JKTCIKL",
    "JKTCIPI","JKTCIPL","JKTCITR","JKTCIXP","JKTDAMG","JKTDJAR","JKTEMPL",
    "JKTEPIC","JKTGADN","JKTGAND","JKTGRPA","JKTGRSE","JKTHOKC","JKTKALI",
    "JKTKASA","JKTKEVI","JKTKRJT","JKTKTM","JKTKUCI","JKTLOSA","JKTMETR",
    "JKTONBE","JKTPGC","JKTPLSE","JKTPOID","JKTPOIN","JKTPURI","JKTSACI",
    "JKTSECI","JKTSETI","JKTSTMO","JKTSTUX","TGRAEBS","TGRALSU","TGRBACI",
    "TGRBAKO","TGRBINT","TGRBIX2","TGRBIXC","TGRCBCI","TGRCICI","TGRCIKU",
    "TGRICWA","TGRKARA","TGRLIWO","TGRLOBI","TGRQBBC","TGRSERO","TGRSERP",
    "TGRTACI","TGRTHBR","TGRTRBI",
}


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=MOBILE_UA, viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()

        # ── STEP 1: Parse sitemap.xml ──────────────────────────────────────
        print("\n" + "="*60)
        print("STEP 1 — Parse semua cinema_id dari sitemap.xml")
        print("="*60)

        r = await page.goto(f"{MOBILE_BASE}/sitemap.xml", wait_until="domcontentloaded", timeout=15_000)
        content = await page.content()

        # Sitemap berisi URL seperti /cinemas/JKTAGT9
        all_sitemap_ids = list(dict.fromkeys(  # preserve order, deduplicate
            re.findall(r'/cinemas/([A-Z]{3}[A-Z0-9]{3,})', content)
        ))

        non_jabodebek_ids = [c for c in all_sitemap_ids if c not in KNOWN_JABODETABEK]
        jabodebek_ids     = [c for c in all_sitemap_ids if c in KNOWN_JABODETABEK]

        print(f"Total cinema_id di sitemap          : {len(all_sitemap_ids)}")
        print(f"Sudah diketahui (Jabodetabek)       : {len(jabodebek_ids)}")
        print(f"Baru (non-Jabodetabek)              : {len(non_jabodebek_ids)}")
        print(f"\nSemua non-Jabodetabek cinema_id ({len(non_jabodebek_ids)}):")
        for cid in non_jabodebek_ids:
            print(f"  {cid}")

        # ── STEP 2: Group by prefix (kota) ────────────────────────────────
        print("\n" + "="*60)
        print("STEP 2 — Grouping by prefix (kota)")
        print("="*60)

        prefix_groups: dict[str, list[str]] = defaultdict(list)
        for cid in all_sitemap_ids:
            prefix_groups[cid[:3]].append(cid)

        print(f"{'Prefix':<8} {'Count':>5}  {'Sample IDs'}")
        print("-" * 60)
        for prefix, ids in sorted(prefix_groups.items(), key=lambda x: -len(x[1])):
            print(f"{prefix:<8} {len(ids):>5}  {', '.join(ids[:5])}")

        # ── STEP 3: Test getTheaterSchedule pada 15 cinema non-Jabodetabek
        print("\n" + "="*60)
        print("STEP 3 — Test getTheaterSchedule (real non-Jabodetabek IDs)")
        print("="*60)

        # Ambil 3 cinema per prefix untuk representasi berbagai kota
        sample_ids: list[str] = []
        for prefix, ids in sorted(prefix_groups.items()):
            if ids[0] not in KNOWN_JABODETABEK:
                sample_ids.extend(ids[:2])
            if len(sample_ids) >= 20:
                break

        print(f"Testing {len(sample_ids)} cinema IDs...\n")

        results = []
        for cid in sample_ids:
            schedule_raw: list[dict] = []
            schedule_url_seen: list[str] = []

            async def cap(resp, sr=schedule_raw, su=schedule_url_seen):
                if "getTheaterSchedule" in resp.url:
                    su.append(resp.url)
                    try:
                        data  = json.loads(await resp.text())
                        value = (data.get("data") or {}).get("value") or []
                        if isinstance(value, list): sr.extend(value)
                    except: pass

            page.on("response", cap)
            try:
                r = await page.goto(
                    f"{MOBILE_BASE}/cinemas/{cid}",
                    wait_until="networkidle", timeout=15_000,
                )
                await page.wait_for_timeout(2000)
            except Exception as e:
                print(f"  {cid}: NAVIGATE ERROR {e}")
                page.remove_listener("response", cap)
                continue
            page.remove_listener("response", cap)

            title = await page.title()
            is_404 = "404" in title

            movie_count = 0
            for day in schedule_raw:
                for subtype_movies in (day.get("cinema") or {}).values():
                    if isinstance(subtype_movies, list):
                        movie_count += len(subtype_movies)

            status = "✅" if not is_404 and schedule_raw else ("❌ 404" if is_404 else "⚠️ no schedule")
            results.append((cid, status, movie_count, schedule_url_seen))
            print(f"  {cid:<12} {status:<15} movies={movie_count:3d}  url={schedule_url_seen[0] if schedule_url_seen else 'none'!r}")

        # ── STEP 4: Cek format URL getTheaterSchedule ─────────────────────
        print("\n" + "="*60)
        print("STEP 4 — Detail getTheaterSchedule response structure")
        print("="*60)

        # Ambil 1 cinema yang sukses
        ok_ids = [r[0] for r in results if "✅" in r[1]]
        if ok_ids:
            test_cid = ok_ids[0]
            detail: list[dict] = []

            async def cap_detail(resp, d=detail):
                if "getTheaterSchedule" in resp.url:
                    try:
                        body = json.loads(await resp.text())
                        d.append(body)
                    except: pass

            page.on("response", cap_detail)
            await page.goto(f"{MOBILE_BASE}/cinemas/{test_cid}", wait_until="networkidle", timeout=15_000)
            await page.wait_for_timeout(2000)
            page.remove_listener("response", cap_detail)

            if detail:
                first = detail[0]
                print(f"\nCinema: {test_cid}")
                print(f"Top-level keys: {list(first.keys())}")
                data_val = (first.get("data") or {}).get("value") or []
                if data_val:
                    day0 = data_val[0]
                    print(f"Day keys: {list(day0.keys())}")
                    print(f"timezone: {day0.get('timezone')}")
                    print(f"date: {day0.get('date')}")
                    cinema_section = day0.get("cinema") or {}
                    print(f"cinema subtypes: {list(cinema_section.keys())}")
                    for stype, movies in cinema_section.items():
                        if isinstance(movies, list) and movies:
                            m = movies[0]
                            print(f"\n  Subtype '{stype}' sample movie keys: {list(m.keys())}")
                            print(f"    cafe_group       : {m.get('cafe_group')}")
                            print(f"    movie_title      : {m.get('movie_title')}")
                            print(f"    dc21_parent_movie_id: {m.get('dc21_parent_movie_id')}")
                            showtimes = m.get("show_times") or m.get("showtimes") or []
                            print(f"    showtimes count  : {len(showtimes)}")
                            if showtimes:
                                print(f"    showtime[0] keys : {list(showtimes[0].keys()) if isinstance(showtimes[0], dict) else showtimes[0]}")

        # ── SUMMARY ──────────────────────────────────────────────────────────
        print("\n" + "="*60)
        print("SUMMARY")
        print("="*60)
        success = [r for r in results if "✅" in r[1]]
        fail    = [r for r in results if "✅" not in r[1]]
        print(f"getTheaterSchedule sukses : {len(success)}/{len(results)}")
        print(f"Gagal/404                 : {len(fail)}/{len(results)}")
        print(f"\nTotal cinema_id dari sitemap: {len(all_sitemap_ids)}")
        print(f"  → Ini adalah sumber LENGKAP untuk scrape semua XXI Indonesia")
        print(f"  → Strategi: fetch sitemap dulu, lalu getTheaterSchedule per cinema_id")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())