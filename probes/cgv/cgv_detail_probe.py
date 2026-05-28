"""
cgv_detail_probe.py
Probe struktur HTML lengkap untuk:
1. Showtimes dari /en/schedule/cinema/{id}
2. Genre + age_rating dari /en/movies/info/{movie_id}

Jalankan: python cgv_detail_probe.py
Output  : cgv_detail_probe_results.json
"""
import asyncio, json, re, urllib.parse
import httpx
from bs4 import BeautifulSoup
from datetime import date

CGV_BASE      = "https://www.cgv.id"
CGV_SCHEDULE  = f"{CGV_BASE}/en/schedule/cinema"
BASE_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}
results = {}

async def probe():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=BASE_HEADERS) as client:

        # ── Warm-up ───────────────────────────────────────────────────────
        print("[1] Warm-up GET /en/...")
        await client.get(f"{CGV_BASE}/en/", headers={**BASE_HEADERS, "Accept": "text/html,*/*"})
        xsrf = urllib.parse.unquote(client.cookies.get("XSRF-TOKEN", ""))
        print(f"  XSRF ready: {len(xsrf)} chars")

        ajax_h = {
            **BASE_HEADERS,
            "Accept": "application/json, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": xsrf,
            "Origin": CGV_BASE,
            "Referer": f"{CGV_SCHEDULE}/002",
            "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Site": "same-origin",
        }

        # ── Step 2: GET /en/schedule/cinema/002 → dump HTML lengkap ──────
        print("\n[2] GET schedule cinema/002 → dump raw HTML blok film...")
        r = await client.get(f"{CGV_SCHEDULE}/002", headers={
            **BASE_HEADERS, "Accept": "text/html,*/*", "Referer": CGV_SCHEDULE,
        })
        print(f"  Status: {r.status_code} | Size: {len(r.text)}")
        soup = BeautifulSoup(r.text, "lxml")

        # Dump semua .schedule-lists block secara lengkap (max 3 film)
        schedule_blocks = soup.select(".schedule-lists")
        print(f"  .schedule-lists count: {len(schedule_blocks)}")
        raw_blocks = []
        for i, blk in enumerate(schedule_blocks[:3]):
            raw_html = str(blk)
            raw_text = blk.get_text("\n", strip=True)
            print(f"\n  ── Block {i+1} RAW HTML (first 2000 chars) ──")
            print(raw_html[:2000])
            print(f"\n  ── Block {i+1} TEXT ──")
            print(raw_text[:600])
            raw_blocks.append({
                "index": i,
                "html": raw_html,
                "text": raw_text,
                "all_classes": sorted({
                    cls for el in blk.find_all(class_=True)
                    for cls in el.get("class", [])
                }),
                "all_data_attrs": {
                    el.name + "." + str(el.get("class","")): {
                        k: v for k, v in el.attrs.items() if k.startswith("data-")
                    }
                    for el in blk.find_all(True)
                    if any(k.startswith("data-") for k in el.attrs)
                },
            })

        results["schedule_blocks_sample"] = raw_blocks
        results["schedule_002_full_html"] = r.text  # HTML penuh untuk analisis manual

        # Semua CSS class unik di halaman
        all_cls = sorted({c for el in soup.find_all(class_=True) for c in el.get("class",[])})
        results["all_css_classes"] = all_cls
        print(f"\n  Semua CSS class: {all_cls}")

        # ── Step 3: Ambil movie IDs dari halaman schedule ─────────────────
        print("\n[3] Extract movie IDs dari halaman schedule...")
        movie_links = soup.select("a[href*='/en/movies/info/']")
        movie_ids = []
        for a in movie_links:
            href = a.get("href","")
            m = re.search(r"/en/movies/info/(\d+)", href)
            if m:
                mid = m.group(1)
                title_param = re.search(r"[?&]title=([^&]+)", href)
                title = urllib.parse.unquote(title_param.group(1)).replace("-"," ").upper() if title_param else ""
                if mid not in [x["id"] for x in movie_ids]:
                    movie_ids.append({"id": mid, "href": href, "title_slug": title})
        print(f"  Found {len(movie_ids)} movie links: {movie_ids[:10]}")
        results["movie_ids_from_schedule"] = movie_ids

        # ── Step 4: GET /en/movies/info/{id} → genre + age_rating ────────
        print("\n[4] GET movie detail pages (3 film pertama)...")
        movie_details = []
        for movie in movie_ids[:3]:
            url = f"{CGV_BASE}/en/movies/info/{movie['id']}"
            r_m = await client.get(url, headers={
                **BASE_HEADERS, "Accept": "text/html,*/*",
                "Referer": f"{CGV_SCHEDULE}/002",
            })
            print(f"\n  Movie {movie['id']} ({movie['title_slug'][:30]})")
            print(f"  Status: {r_m.status_code} | Size: {len(r_m.text)}")

            soup_m = BeautifulSoup(r_m.text, "lxml")

            # Dump semua class unik yang relevan di halaman movie
            movie_cls = sorted({c for el in soup_m.find_all(class_=True) for c in el.get("class",[])
                                 if any(kw in c.lower() for kw in
                                        ["genre","rating","age","info","detail","movie","film","cast"])})
            print(f"  Relevant CSS classes: {movie_cls}")

            # Coba berbagai selector untuk genre & age_rating
            selectors_to_try = [
                ".genre", ".genres", "[class*='genre']",
                ".age-rating", ".age-rate", ".rating", "[class*='rating']", "[class*='age']",
                ".movie-info", ".movie-detail", "[class*='movie-info']",
                ".info-list", ".detail-info", "table.info",
                ".classification", "[class*='classif']",
                "dd", "dt",
            ]
            for sel in selectors_to_try:
                els = soup_m.select(sel)
                if els:
                    texts = [e.get_text(strip=True)[:80] for e in els[:3]]
                    print(f"    {sel}: {texts}")

            # Dump meta tags
            metas = {m.get("name","") or m.get("property",""): m.get("content","")
                     for m in soup_m.select("meta[content]")
                     if m.get("name") or m.get("property")}
            print(f"  Meta tags: {json.dumps({k:v[:80] for k,v in list(metas.items())[:15]}, ensure_ascii=False)}")

            # Dump JSON-LD / structured data
            for script in soup_m.select("script[type='application/ld+json']"):
                try:
                    ld = json.loads(script.string or "")
                    print(f"  JSON-LD: {json.dumps(ld, ensure_ascii=False)[:400]}")
                except: pass

            # Ambil full HTML untuk analisis manual
            movie_details.append({
                "id": movie["id"],
                "title_slug": movie["title_slug"],
                "url": url,
                "status": r_m.status_code,
                "html": r_m.text,
                "relevant_classes": movie_cls,
                "meta": metas,
            })
            await asyncio.sleep(0.5)

        results["movie_detail_pages"] = movie_details

        # ── Step 5: POST home_movie_list → inspect full JSON ─────────────
        print("\n[5] POST home_movie_list → full JSON response cinema 002...")
        r_ldr = await client.post(
            f"{CGV_BASE}/en/loader/home_movie_list",
            data={"cinema_id": "002"}, headers=ajax_h,
        )
        print(f"  Status: {r_ldr.status_code}")
        try:
            ldr_json = r_ldr.json()
            print(f"  Keys: {list(ldr_json.keys())}")
            for k, v in ldr_json.items():
                print(f"  [{k}] ({len(str(v))} chars): {str(v)[:400]}")
            results["loader_full_response"] = ldr_json
        except Exception as e:
            print(f"  Parse error: {e}")
            results["loader_raw"] = r_ldr.text[:2000]

        # ── Step 6: Inspect showtime HTML dalam schedule block ────────────
        print("\n[6] Inspect showtime elements dalam .schedule-lists...")
        for i, blk in enumerate(schedule_blocks[:2]):
            print(f"\n  Block {i+1}:")
            # Semua elemen dengan class yang mengandung 'show' atau 'time'
            for el in blk.find_all(class_=True):
                classes = el.get("class", [])
                if any("show" in c or "time" in c for c in classes):
                    print(f"    <{el.name} class='{' '.join(classes)}'> {el.get_text(strip=True)[:80]}")
            # Semua elemen <a> dengan jam (pola HH:MM)
            times_found = []
            for el in blk.find_all(text=re.compile(r'\d{1,2}:\d{2}')):
                times_found.append(el.strip())
            print(f"    Time texts: {times_found}")
            # data-* attributes
            for el in blk.find_all(True):
                data = {k:v for k,v in el.attrs.items() if k.startswith("data-")}
                if data:
                    print(f"    <{el.name} {data}> {el.get_text(strip=True)[:40]}")

    # ── Save ──────────────────────────────────────────────────────────────────
    with open("cgv_detail_probe_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print("\n✅ Saved: cgv_detail_probe_results.json")

if __name__ == "__main__":
    asyncio.run(probe())