"""
cgv_probe.py
Script probe untuk memetakan endpoint CGV Indonesia.
Jalankan dari komputer lokal (bukan server/VPS).

Usage:
    pip install httpx beautifulsoup4 lxml rich
    python cgv_probe.py
"""
import asyncio
import json
import re
import httpx
from bs4 import BeautifulSoup
from datetime import date

CGV_BASE = "https://www.cgv.id"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


async def probe():
    results = {}

    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:

        # ── Step 1: GET homepage → dapat cookies ──────────────────────────
        print("\n[1/6] GET homepage untuk cookies & CSRF token...")
        r = await client.get(f"{CGV_BASE}/en/")
        print(f"  Status: {r.status_code} | Cookies: {dict(client.cookies)}")
        results["homepage_status"] = r.status_code
        results["cookies"] = dict(client.cookies)

        # Extract CSRF token dari HTML / meta tag / JS
        soup = BeautifulSoup(r.text, "lxml")
        csrf = (
            (soup.find("meta", {"name": "csrf-token"}) or {}).get("content")
            or (soup.find("input", {"name": "csrf_token"}) or {}).get("value")
            or (soup.find("input", {"name": "_token"}) or {}).get("value")
        )
        # Cari dari JS inline
        if not csrf:
            m = re.search(r'csrf[_-]?token["\s]*[:=]["\s]*([a-f0-9]{32,})', r.text, re.I)
            if m:
                csrf = m.group(1)
        print(f"  CSRF token: {csrf or '(not found)'}")
        results["csrf_token"] = csrf

        # Extract semua URL dari JS yang menarik
        js_urls = re.findall(r'(?:url|action|href)["\s]*[:=]["\s]*(\/en\/[^"\'<>\s]{3,60})', r.text)
        print(f"  JS URLs found: {len(js_urls)}")
        for u in sorted(set(js_urls))[:20]:
            print(f"    {u}")
        results["js_urls"] = list(set(js_urls))

        # ── Step 2: GET /en/schedule/cinema → struktur cinema list ────────
        print("\n[2/6] GET /en/schedule/cinema...")
        r2 = await client.get(f"{CGV_BASE}/en/schedule/cinema", headers={
            **HEADERS,
            "Referer": f"{CGV_BASE}/en/",
        })
        print(f"  Status: {r2.status_code} | Size: {len(r2.text)} chars")
        results["schedule_cinema_status"] = r2.status_code

        soup2 = BeautifulSoup(r2.text, "lxml")
        # Extract cinema IDs dari links
        cinema_ids = []
        for a in soup2.select("a[href*='/en/schedule/cinema/']"):
            m = re.search(r"/en/schedule/cinema/(\w+)", a["href"])
            if m:
                cinema_ids.append(m.group(1))
        print(f"  Cinema IDs found: {len(cinema_ids)} -> {cinema_ids[:10]}")
        results["cinema_ids_from_html"] = cinema_ids

        # Extract JS dari halaman ini juga
        js_urls2 = re.findall(r'(?:url|action|href)["\s]*[:=]["\s]*(\/en\/[^"\'<>\s]{3,60})', r2.text)
        for u in sorted(set(js_urls2)):
            print(f"    {u}")

        # Cari script src
        for script in soup2.select("script[src]"):
            src = script.get("src", "")
            if "cgv" in src.lower() or "/en/" in src:
                print(f"  Script src: {src}")

        # ── Step 3: POST /en/execute dengan berbagai payload ──────────────
        print("\n[3/6] POST /en/execute — probe payload...")

        execute_url = f"{CGV_BASE}/en/execute"
        post_headers = {
            **HEADERS,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{CGV_BASE}/en/schedule/cinema",
            "Origin": CGV_BASE,
        }
        if csrf:
            post_headers["X-CSRF-TOKEN"] = csrf

        execute_payloads = [
            # Pola umum CodeIgniter cinema app
            {"action": "get_cinema_list"},
            {"action": "cinema_list"},
            {"action": "get_cities"},
            {"action": "city_list"},
            {"action": "get_area"},
            {"action": "area_list"},
            {"type": "cinema"},
            {"type": "city"},
            {"cmd": "cinema"},
            # Dengan area/city parameter
            {"action": "get_cinema_list", "area_id": "1"},
            {"action": "get_cinema_list", "city_id": "JKT"},
            {"action": "cinema_by_area", "area": "jakarta"},
            # Kosong
            {},
        ]

        execute_results = []
        for payload in execute_payloads:
            try:
                r_ex = await client.post(execute_url, data=payload, headers=post_headers)
                ct = r_ex.headers.get("content-type", "")
                body = r_ex.text[:200].replace("\n", " ")
                entry = {
                    "payload": payload,
                    "status": r_ex.status_code,
                    "content_type": ct,
                    "body_size": len(r_ex.text),
                    "body_preview": body,
                }
                execute_results.append(entry)
                is_json = "json" in ct or r_ex.text.strip().startswith(("[", "{"))
                marker = "🟢 JSON!" if is_json and r_ex.status_code == 200 else ""
                print(f"  {payload} -> {r_ex.status_code} | {len(r_ex.text)}c | {ct[:30]} | {body[:80]} {marker}")
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"  {payload} -> ERROR: {e}")

        results["execute_probe"] = execute_results

        # ── Step 4: GET script JS utama untuk inspect action names ────────
        print("\n[4/6] Cari dan baca file JS utama CGV...")
        js_files = []
        for script in soup2.select("script[src]"):
            src = script.get("src", "")
            if not src:
                continue
            full = src if src.startswith("http") else f"{CGV_BASE}{src}"
            js_files.append(full)
            print(f"  Found JS: {full}")

        for js_url in js_files[:5]:
            try:
                r_js = await client.get(js_url, headers={**HEADERS, "Accept": "*/*"})
                if r_js.status_code == 200:
                    # Cari action names, endpoint references
                    actions = re.findall(r'action["\s]*[:=]["\s]*["\']([a-z_]+)["\']', r_js.text, re.I)
                    loaders = re.findall(r'loader\/([a-z_]+)', r_js.text)
                    execute_refs = re.findall(r'en\/execute[^"\']{0,50}', r_js.text)
                    print(f"  {js_url} -> {r_js.status_code} | {len(r_js.text)}c")
                    print(f"    Actions: {list(set(actions))[:15]}")
                    print(f"    Loaders: {list(set(loaders))[:10]}")
                    print(f"    Execute refs: {execute_refs[:5]}")
                    results[f"js_{js_url.split('/')[-1]}"] = {
                        "actions": list(set(actions)),
                        "loaders": list(set(loaders)),
                    }
            except Exception as e:
                print(f"  JS fetch error: {e}")

        # ── Step 5: Probe /en/loader/home_movie_list ──────────────────────
        print("\n[5/6] POST /en/loader/home_movie_list — probe payload...")
        movie_url = f"{CGV_BASE}/en/loader/home_movie_list"
        today = date.today().strftime("%Y-%m-%d")

        test_cinema_id = cinema_ids[0] if cinema_ids else "002"
        movie_payloads = [
            {"cinema_id": test_cinema_id},
            {"cinema_id": test_cinema_id, "date": today},
            {"id": test_cinema_id},
            {"id": test_cinema_id, "date": today},
            {"cinemaId": test_cinema_id},
            {"cinema": test_cinema_id},
            {"cinema_id": test_cinema_id, "schedule_date": today},
            {"cinema_id": test_cinema_id, "type": "schedule"},
            {},
        ]
        movie_results = []
        for payload in movie_payloads:
            try:
                r_mv = await client.post(movie_url, data=payload, headers=post_headers)
                ct = r_mv.headers.get("content-type", "")
                body = r_mv.text[:200].replace("\n", " ")
                entry = {
                    "payload": payload,
                    "status": r_mv.status_code,
                    "content_type": ct,
                    "body_size": len(r_mv.text),
                    "body_preview": body,
                }
                movie_results.append(entry)
                is_json = "json" in ct or r_mv.text.strip().startswith(("[", "{"))
                marker = "🟢 JSON!" if is_json and r_mv.status_code == 200 else ""
                print(f"  {payload} -> {r_mv.status_code} | {len(r_mv.text)}c | {ct[:30]} | {body[:80]} {marker}")
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"  {payload} -> ERROR: {e}")
        results["movie_loader_probe"] = movie_results

        # ── Step 6: Probe loader variants ────────────────────────────────
        print("\n[6/6] Probe variasi /en/loader/...")
        loader_variants = [
            "cinema_list", "city_list", "area_list",
            "schedule_list", "movie_list", "now_playing",
            "home_cinema_list", "home_schedule",
            "schedule_by_cinema", "movie_by_cinema",
        ]
        for lv in loader_variants:
            url = f"{CGV_BASE}/en/loader/{lv}"
            try:
                r_lv = await client.post(url, data={"cinema_id": test_cinema_id}, headers=post_headers)
                ct = r_lv.headers.get("content-type", "")
                body = r_lv.text[:100].replace("\n", " ")
                is_json = "json" in ct or r_lv.text.strip().startswith(("[", "{"))
                marker = "🟢 JSON!" if is_json and r_lv.status_code == 200 else ""
                print(f"  /en/loader/{lv} -> {r_lv.status_code} | {len(r_lv.text)}c | {body[:60]} {marker}")
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"  /en/loader/{lv} -> ERROR: {e}")

    # ── Save results ──────────────────────────────────────────────────────────
    out_file = "cgv_probe_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n✅ Hasil lengkap disimpan di: {out_file}")
    print("\n📋 Paste isi file tersebut ke chat agar bisa dianalisis lebih lanjut.")


if __name__ == "__main__":
    asyncio.run(probe())