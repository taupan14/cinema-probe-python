"""
cgv_price_probe.py
Probe untuk menemukan studio_id dan ticket_price di CGV.

Kandidat sumber:
1. HTML .schedule-lists — attr-* di anchor showtime
2. .price_section / .table_price di halaman schedule
3. POST /en/execute dengan action terkait harga
4. GET /en/schedule/cinema/{id} dengan date berbeda
5. Klik showtime → redirect ke booking page

Jalankan: python cgv_price_probe.py
Output  : cgv_price_probe_results.json
"""
import asyncio, json, re, urllib.parse
import httpx
from bs4 import BeautifulSoup

CGV_BASE     = "https://www.cgv.id"
CGV_SCHEDULE = f"{CGV_BASE}/en/schedule/cinema"
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}
results = {}

async def probe():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=BASE_HEADERS) as client:

        # ── Warm-up ───────────────────────────────────────────────────────
        await client.get(f"{CGV_BASE}/en/", headers={**BASE_HEADERS, "Accept": "text/html,*/*"})
        xsrf = urllib.parse.unquote(client.cookies.get("XSRF-TOKEN", ""))
        ajax_h = {
            **BASE_HEADERS,
            "Accept": "application/json, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": xsrf,
            "Origin": CGV_BASE,
            "Referer": f"{CGV_SCHEDULE}/002",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Site": "same-origin",
        }
        print(f"[✓] Warm-up OK | XSRF: {len(xsrf)} chars")

        # ── Step 1: Dump FULL HTML schedule cinema 002 ────────────────────
        print("\n[1] GET /en/schedule/cinema/002 — cari price & studio elements...")
        r = await client.get(f"{CGV_SCHEDULE}/002", headers={
            **BASE_HEADERS, "Accept": "text/html,*/*", "Referer": CGV_SCHEDULE,
        })
        soup = BeautifulSoup(r.text, "lxml")

        # Cari elemen price
        price_related = []
        for el in soup.find_all(class_=True):
            classes = " ".join(el.get("class", []))
            if any(kw in classes.lower() for kw in ["price", "harga", "ticket", "studio", "audi"]):
                price_related.append({
                    "tag": el.name,
                    "class": classes,
                    "text": el.get_text(strip=True)[:200],
                    "html": str(el)[:300],
                })
        print(f"  Price/studio elements: {len(price_related)}")
        for p in price_related[:10]:
            print(f"    <{p['tag']} class='{p['class']}'> {p['text'][:100]}")
        results["price_elements_schedule"] = price_related

        # Dump semua attr-* di showtime anchors
        print("\n  Showtime anchor attributes:")
        showtime_attrs = []
        for a in soup.select(".showtime-lists li a"):
            attrs = dict(a.attrs)
            showtime_attrs.append(attrs)
            print(f"    {attrs}")
        results["showtime_anchor_attrs"] = showtime_attrs[:20]

        # Cek .price_section dan .table_price (ada di CSS classes dari probe sebelumnya)
        for cls in ["price_section", "table_price", "price_group", "price_audi",
                    "sub_group_price", "col-prices-cinema-sch", "title-price-info"]:
            els = soup.select(f".{cls}")
            if els:
                print(f"\n  .{cls} ({len(els)} elements):")
                for el in els[:3]:
                    print(f"    HTML: {str(el)[:400]}")
                results[f"class_{cls}"] = [str(el) for el in els[:5]]

        # ── Step 2: Cari showtime_id dan test endpoint booking ────────────
        print("\n[2] Extract showtime IDs dan test booking endpoint...")
        showtime_ids = []
        for a in soup.select(".showtime-lists li a[id]"):
            sid = a.get("id", "")
            fmt = a.get("attr-fmt", "")
            mov = a.get("attr-mov", "")
            hall = a.get("attr-audi-type-name", "")
            time = a.get_text(strip=True)
            if sid:
                showtime_ids.append({"id": sid, "fmt": fmt, "mov": mov, "hall": hall, "time": time})
        print(f"  Found {len(showtime_ids)} showtime IDs: {showtime_ids[:5]}")
        results["showtime_ids"] = showtime_ids

        # Test endpoint booking dengan showtime_id pertama
        if showtime_ids:
            test_id = showtime_ids[0]["id"]
            print(f"\n  Testing booking endpoints with showtime_id={test_id}...")

            booking_endpoints = [
                ("GET",  f"{CGV_BASE}/en/booking/{test_id}",                   {}),
                ("GET",  f"{CGV_BASE}/en/schedule/detail/{test_id}",            {}),
                ("GET",  f"{CGV_BASE}/en/movies/schedule/{test_id}",            {}),
                ("POST", f"{CGV_BASE}/en/execute",                              {"action": "get_schedule_detail", "schedule_id": test_id}),
                ("POST", f"{CGV_BASE}/en/execute",                              {"action": "get_seat_price", "schedule_id": test_id}),
                ("POST", f"{CGV_BASE}/en/execute",                              {"action": "get_studio_info", "schedule_id": test_id}),
                ("POST", f"{CGV_BASE}/en/execute",                              {"action": "schedule_detail", "id": test_id}),
                ("POST", f"{CGV_BASE}/en/loader/schedule_detail",               {"schedule_id": test_id}),
                ("POST", f"{CGV_BASE}/en/loader/get_price",                     {"schedule_id": test_id, "cinema_id": "002"}),
                ("POST", f"{CGV_BASE}/en/loader/ticket_price",                  {"schedule_id": test_id}),
                ("POST", f"{CGV_BASE}/en/loader/seat_price",                    {"schedule_id": test_id, "cinema_id": "002"}),
                ("POST", f"{CGV_BASE}/en/loader/studio",                        {"schedule_id": test_id, "cinema_id": "002"}),
            ]

            booking_results = []
            for method, url, data in booking_endpoints:
                try:
                    if method == "GET":
                        r_b = await client.get(url, headers={**BASE_HEADERS, "Accept": "text/html,*/*",
                                                              "Referer": f"{CGV_SCHEDULE}/002"})
                    else:
                        r_b = await client.post(url, data=data, headers=ajax_h)

                    ct   = r_b.headers.get("content-type", "")
                    body = r_b.text
                    marker = "🟢 DATA!" if r_b.status_code == 200 and len(body) > 50 and "price" in body.lower() else \
                             "🔵 OK!" if r_b.status_code == 200 and len(body) > 50 else ""
                    print(f"  {method} {url.replace(CGV_BASE,'')} {data} → {r_b.status_code} | {len(body)}c {marker}")
                    if len(body) > 50:
                        print(f"    body: {body[:300]}")
                    booking_results.append({
                        "method": method, "url": url, "data": data,
                        "status": r_b.status_code, "body": body[:500],
                        "content_type": ct,
                    })
                    await asyncio.sleep(0.3)
                except Exception as e:
                    print(f"  ERROR {url}: {e}")
            results["booking_endpoints"] = booking_results

        # ── Step 3: Dump .price_section HTML lengkap ─────────────────────
        print("\n[3] Dump .col-prices-cinema-sch & price sections...")
        price_section = soup.select_one(".col-prices-cinema-sch, .price_section, [class*='price']")
        if price_section:
            print(f"  Found: {str(price_section)[:1000]}")
            results["price_section_html"] = str(price_section)
        else:
            print("  (not found in schedule page)")

        # ── Step 4: GET halaman cinema info (bukan schedule) ─────────────
        print("\n[4] GET /en/cinemas/info/002 atau endpoint info lain...")
        info_urls = [
            f"{CGV_BASE}/en/cinemas/info/002",
            f"{CGV_BASE}/en/cinemas/002",
            f"{CGV_BASE}/en/cinema/002",
            f"{CGV_BASE}/en/cinema-info/002",
        ]
        for url in info_urls:
            r_i = await client.get(url, headers={**BASE_HEADERS, "Accept": "text/html,*/*",
                                                  "Referer": CGV_SCHEDULE})
            print(f"  {url} → {r_i.status_code} | {len(r_i.text)}c")
            if r_i.status_code == 200 and len(r_i.text) > 200:
                s = BeautifulSoup(r_i.text, "lxml")
                price_els = s.select("[class*='price'], [class*='ticket'], [class*='studio']")
                print(f"    price elements: {len(price_els)}")
                for el in price_els[:3]:
                    print(f"    {el.get_text(strip=True)[:100]}")
                results[f"cinema_info_{url.split('/')[-1]}"] = r_i.text[:3000]
            await asyncio.sleep(0.3)

        # ── Step 5: POST /en/execute action terkait studio/price ─────────
        print("\n[5] POST /en/execute — price & studio actions...")
        price_actions = [
            {"action": "get_price_list", "cinema_id": "002"},
            {"action": "price_list",     "cinema_id": "002"},
            {"action": "get_prices",     "cinema_id": "002"},
            {"action": "get_ticket_price","cinema_id": "002"},
            {"action": "studio_list",    "cinema_id": "002"},
            {"action": "get_studio",     "cinema_id": "002"},
            {"action": "audi_list",      "cinema_id": "002"},
        ]
        execute_results = []
        for payload in price_actions:
            r_e = await client.post(f"{CGV_BASE}/en/execute", data=payload, headers=ajax_h)
            body = r_e.text
            marker = "🟢 DATA!" if r_e.status_code == 200 and len(body) > 33 else ""
            print(f"  {payload} → {r_e.status_code} | {len(body)}c | {body[:150]} {marker}")
            execute_results.append({"payload": payload, "status": r_e.status_code, "body": body})
            await asyncio.sleep(0.3)
        results["execute_price_actions"] = execute_results

        # ── Step 6: Inspect script.js untuk price-related function ───────
        print("\n[6] Baca script.js — cari fungsi price/studio...")
        r_js = await client.get(
            f"https://cdn.cgv.id/assets/js/script.js?v=0.0.19",
            headers={**BASE_HEADERS, "Accept": "*/*", "Referer": f"{CGV_SCHEDULE}/002"},
        )
        if r_js.status_code == 200:
            js = r_js.text
            # Cari fungsi/variabel terkait price dan studio
            price_funcs = re.findall(r'(?:function|var|let|const)\s+\w*[Pp]rice\w*[^;{]{0,100}', js)
            studio_funcs = re.findall(r'(?:function|var|let|const)\s+\w*[Ss]tudio\w*[^;{]{0,100}', js)
            price_urls = re.findall(r'["\']([^"\']*(?:price|studio|ticket)[^"\']*)["\']', js, re.I)
            ajax_calls = re.findall(r'url\s*:\s*["\']([^"\']+)["\']', js)

            print(f"  JS size: {len(js)} chars")
            print(f"  Price functions: {price_funcs[:5]}")
            print(f"  Studio functions: {studio_funcs[:5]}")
            print(f"  Price-related strings: {price_urls[:10]}")
            print(f"  AJAX urls: {[u for u in ajax_calls if 'cgv' in u.lower() or '/en/' in u][:10]}")

            results["js_price_analysis"] = {
                "price_funcs": price_funcs[:10],
                "studio_funcs": studio_funcs[:10],
                "price_urls": price_urls[:20],
                "ajax_urls": ajax_calls[:30],
                "js_size": len(js),
            }
        else:
            print(f"  script.js → {r_js.status_code}")

    # ── Save ──────────────────────────────────────────────────────────────────
    with open("cgv_price_probe_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print("\n✅ Saved: cgv_price_probe_results.json")
    print("📋 Paste output terminal ini ke chat.")

if __name__ == "__main__":
    asyncio.run(probe())