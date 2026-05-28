"""
cgv_movie_probe.py
Probe mendalam untuk /en/loader/home_movie_list dan /en/execute.
Jalankan dari komputer lokal.

Usage:
    python cgv_movie_probe.py
    
Output: cgv_movie_probe_results.json
"""
import asyncio
import json
import re
import urllib.parse
import httpx
from bs4 import BeautifulSoup
from datetime import date

CGV_BASE         = "https://www.cgv.id"
CGV_SCHEDULE     = f"{CGV_BASE}/en/schedule/cinema"
CGV_MOVIE_LOADER = f"{CGV_BASE}/en/loader/home_movie_list"
CGV_EXECUTE      = f"{CGV_BASE}/en/execute"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}

TEST_CINEMA_IDS = ["002", "003", "006"]  # Grand Indonesia, Pacific Place, Central Park
TODAY = date.today().strftime("%Y-%m-%d")
results = {}


async def probe():
    async with httpx.AsyncClient(
        timeout=20,
        follow_redirects=True,
        headers=BASE_HEADERS,
    ) as client:

        # ── Step 1: Warm-up + ambil XSRF ─────────────────────────────────
        print("\n[1] GET homepage → cookies...")
        r = await client.get(f"{CGV_BASE}/en/", headers={
            **BASE_HEADERS,
            "Accept": "text/html,*/*;q=0.9",
        })
        print(f"  Status: {r.status_code}")
        print(f"  Cookies: {dict(client.cookies)}")

        xsrf_raw = client.cookies.get("XSRF-TOKEN", "")
        xsrf     = urllib.parse.unquote(xsrf_raw)
        print(f"  XSRF-TOKEN (decoded, {len(xsrf)} chars): {xsrf[:60]}...")
        results["step1_cookies"] = dict(client.cookies)
        results["xsrf_decoded_len"] = len(xsrf)

        # ── Step 2: GET schedule page → ambil HTML cinema 002 ────────────
        print("\n[2] GET /en/schedule/cinema/002 → inspect HTML...")
        r2 = await client.get(f"{CGV_SCHEDULE}/002", headers={
            **BASE_HEADERS,
            "Accept": "text/html,*/*;q=0.9",
            "Referer": f"{CGV_BASE}/en/",
        })
        print(f"  Status: {r2.status_code} | Size: {len(r2.text)} chars")
        results["schedule_002_status"] = r2.status_code
        results["schedule_002_size"]   = len(r2.text)

        soup = BeautifulSoup(r2.text, "lxml")

        # Simpan HTML penuh untuk analisis
        results["schedule_002_html_head"] = r2.text[:3000]
        results["schedule_002_html_tail"] = r2.text[-2000:]

        # Cari semua script src
        scripts = [s.get("src","") for s in soup.select("script[src]") if s.get("src")]
        print(f"  Script files: {scripts}")
        results["schedule_002_scripts"] = scripts

        # Cari referensi ke loader/execute di inline scripts
        inline_refs = []
        for script in soup.select("script:not([src])"):
            content = script.string or ""
            if "loader" in content or "execute" in content or "cinema_id" in content:
                inline_refs.append(content[:500])
                print(f"  Inline JS ref: {content[:200]}")
        results["inline_js_refs"] = inline_refs

        # Cari data-* attributes yang mengandung cinema info
        data_attrs = []
        for el in soup.select("[data-cinema], [data-id], [data-cinema-id], [data-url]"):
            data_attrs.append({
                "tag": el.name,
                "attrs": {k:v for k,v in el.attrs.items() if k.startswith("data-")},
                "text": el.get_text()[:50],
            })
        print(f"  data-* elements: {len(data_attrs)}")
        results["data_attrs"] = data_attrs[:20]

        # Cari form yang mungkin POST ke loader
        forms = []
        for form in soup.select("form"):
            forms.append({
                "action": form.get("action",""),
                "method": form.get("method",""),
                "inputs": [{
                    "name": i.get("name",""),
                    "value": i.get("value",""),
                    "type": i.get("type",""),
                } for i in form.select("input,select")],
            })
        print(f"  Forms: {forms}")
        results["forms"] = forms

        # ── Step 3: Baca JS utama CGV → cari action names ─────────────────
        print("\n[3] Baca JS files CGV untuk action names...")
        js_analysis = {}
        for js_src in scripts:
            if not js_src or "google" in js_src or "jquery" in js_src.lower():
                continue
            full_url = js_src if js_src.startswith("http") else f"{CGV_BASE}{js_src}"
            try:
                rjs = await client.get(full_url, headers={**BASE_HEADERS, "Accept": "*/*",
                    "Referer": f"{CGV_SCHEDULE}/002"})
                if rjs.status_code != 200:
                    continue
                content = rjs.text
                
                # Cari semua referensi ke endpoint
                loader_calls = re.findall(r'["\'](?:\/en\/)?loader\/([^"\'/?]{2,40})["\']', content)
                execute_calls = re.findall(r'action["\s:=]+["\']([a-z_]{3,40})["\']', content, re.I)
                cinema_id_refs = re.findall(r'cinema_id["\s:=]+([^,;\n"\']{1,30})', content)
                ajax_calls = re.findall(r'(?:url|URL)["\s:=]+["\']([^"\']{5,80})["\']', content)
                post_data = re.findall(r'data["\s:=]+\{([^}]{5,200})\}', content)
                
                print(f"  {js_src.split('/')[-1][:40]}:")
                print(f"    loaders  : {list(set(loader_calls))}")
                print(f"    actions  : {list(set(execute_calls))[:10]}")
                print(f"    ajax_urls: {[u for u in set(ajax_calls) if 'cgv' in u.lower() or '/en/' in u][:5]}")
                
                js_analysis[js_src] = {
                    "loaders": list(set(loader_calls)),
                    "actions": list(set(execute_calls)),
                    "cinema_id_refs": list(set(cinema_id_refs))[:5],
                    "ajax_urls": list(set(ajax_calls))[:10],
                    "post_data_patterns": post_data[:5],
                    "content_size": len(content),
                }
            except Exception as e:
                print(f"  Error {js_src}: {e}")
        results["js_analysis"] = js_analysis

        # ── Step 4: POST home_movie_list dengan XSRF yang benar ───────────
        print("\n[4] POST /en/loader/home_movie_list dengan XSRF benar...")
        ajax_headers = {
            **BASE_HEADERS,
            "Accept":           "application/json, text/javascript, */*; q=0.01",
            "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN":     xsrf,
            "Origin":           CGV_BASE,
            "Referer":          f"{CGV_SCHEDULE}/002",
            "Sec-Fetch-Dest":   "empty",
            "Sec-Fetch-Mode":   "cors",
            "Sec-Fetch-Site":   "same-origin",
        }

        movie_probe = []
        payloads = [
            {"cinema_id": "002"},
            {"cinema_id": "002", "date": TODAY},
            {"cinema_id": "002", "schedule_date": TODAY},
            {"cinema_id": "002", "type": "now_showing"},
            {"cinema_id": "002", "flag": "1"},
            # Coba tanpa cinema_id
            {},
            # Coba GET
        ]

        for payload in payloads:
            r_mv = await client.post(CGV_MOVIE_LOADER, data=payload, headers=ajax_headers)
            ct   = r_mv.headers.get("content-type","")
            body = r_mv.text
            entry = {
                "payload":        payload,
                "status":         r_mv.status_code,
                "content_type":   ct,
                "body_size":      len(body),
                "body_full":      body,           # simpan FULL response
                "response_headers": dict(r_mv.headers),
            }
            movie_probe.append(entry)
            print(f"  {payload} -> {r_mv.status_code} | {len(body)}c | ct={ct[:40]}")
            print(f"    body: {body[:300]}")
            print()
            await asyncio.sleep(0.5)

        # Coba GET juga
        r_get = await client.get(CGV_MOVIE_LOADER, params={"cinema_id":"002"}, headers={
            **ajax_headers,
            "Accept": "text/html,*/*",
        })
        movie_probe.append({
            "payload": "GET?cinema_id=002",
            "status": r_get.status_code,
            "content_type": r_get.headers.get("content-type",""),
            "body_size": len(r_get.text),
            "body_full": r_get.text,
        })
        print(f"  GET?cinema_id=002 -> {r_get.status_code} | {len(r_get.text)}c")
        print(f"    body: {r_get.text[:300]}")
        results["movie_loader_probe"] = movie_probe

        # ── Step 5: POST /en/execute dengan XSRF benar ───────────────────
        print("\n[5] POST /en/execute dengan XSRF benar...")
        execute_probe = []
        execute_payloads = [
            {"action": "get_cinema_list"},
            {"action": "cinema_list"},
            {"action": "get_now_playing"},
            {"action": "now_playing", "cinema_id": "002"},
            {"action": "get_schedule", "cinema_id": "002"},
            {"action": "schedule", "cinema_id": "002"},
            {"action": "get_movie", "cinema_id": "002"},
            {"action": "movie_list", "cinema_id": "002"},
            {"action": "get_movie_list", "cinema_id": "002"},
            {"action": "home_movie_list", "cinema_id": "002"},
            {"action": "get_schedule_list", "cinema_id": "002", "date": TODAY},
        ]
        for payload in execute_payloads:
            r_ex = await client.post(CGV_EXECUTE, data=payload, headers=ajax_headers)
            ct   = r_ex.headers.get("content-type","")
            body = r_ex.text
            entry = {
                "payload":      payload,
                "status":       r_ex.status_code,
                "content_type": ct,
                "body_size":    len(body),
                "body_full":    body,
            }
            execute_probe.append(entry)
            marker = "🟢 DATA!" if r_ex.status_code == 200 and len(body) > 50 else ""
            print(f"  {payload} -> {r_ex.status_code} | {len(body)}c | {body[:150]} {marker}")
            await asyncio.sleep(0.5)
        results["execute_probe"] = execute_probe

        # ── Step 6: Coba endpoint loader lain yang mungkin ───────────────
        print("\n[6] Probe variasi loader endpoint...")
        loader_variants = [
            "schedule_list",
            "movie_schedule",
            "cinema_schedule",
            "now_playing",
            "movie_list",
            "home_schedule",
            "schedule_by_cinema",
            "cinema_movie",
            "get_schedule",
        ]
        loader_probe = []
        for lv in loader_variants:
            url  = f"{CGV_BASE}/en/loader/{lv}"
            r_lv = await client.post(url, data={"cinema_id":"002", "date": TODAY},
                                     headers=ajax_headers)
            body = r_lv.text
            entry = {
                "url":          url,
                "status":       r_lv.status_code,
                "content_type": r_lv.headers.get("content-type",""),
                "body_size":    len(body),
                "body_full":    body,
            }
            loader_probe.append(entry)
            marker = "🟢 DATA!" if r_lv.status_code == 200 and len(body) > 50 else ""
            print(f"  /en/loader/{lv} -> {r_lv.status_code} | {len(body)}c | {body[:100]} {marker}")
            await asyncio.sleep(0.3)
        results["loader_variants_probe"] = loader_probe

        # ── Step 7: GET langsung halaman schedule cinema 002 ─────────────
        print("\n[7] GET /en/schedule/cinema/002 raw HTML analysis...")
        r7 = await client.get(f"{CGV_SCHEDULE}/002", headers={
            **BASE_HEADERS,
            "Accept": "text/html,*/*",
            "Referer": CGV_SCHEDULE,
        })
        body7 = r7.text
        results["cinema_002_full_html"] = body7

        # Cek apakah ada film di HTML langsung
        soup7  = BeautifulSoup(body7, "lxml")
        movies_in_html = []
        for el in soup7.select("[class*='movie'], [class*='film'], [class*='schedule']"):
            txt = el.get_text()[:100].strip()
            if txt and len(txt) > 3:
                movies_in_html.append({"class": el.get("class"), "text": txt})
        print(f"  Movie-related elements in HTML: {len(movies_in_html)}")
        for m in movies_in_html[:10]:
            print(f"    {m}")
        results["movies_in_html"] = movies_in_html[:30]

        # Cek semua class yang ada di halaman
        all_classes = set()
        for el in soup7.find_all(class_=True):
            for cls in el.get("class", []):
                all_classes.add(cls)
        movie_classes = [c for c in sorted(all_classes)
                         if any(kw in c.lower() for kw in ["movie","film","schedule","show","now"])]
        print(f"  Movie-related CSS classes: {movie_classes}")
        results["movie_css_classes"] = movie_classes

    # ── Save ──────────────────────────────────────────────────────────────────
    with open("cgv_movie_probe_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print("\n✅ Saved: cgv_movie_probe_results.json")
    print("📋 Paste ke chat untuk analisis lebih lanjut.")


if __name__ == "__main__":
    asyncio.run(probe())