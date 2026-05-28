"""
probe_api.py
============
Probe semua endpoint API yang ditemukan dari diagnosa.
Jalankan ini untuk lihat struktur JSON response sebelum nulis scraper.

Usage:
    python probe_api.py

Output:
    - probe_output/*.json  → response dari setiap endpoint
    - Log di terminal      → ringkasan struktur data
"""

import asyncio
import json
from pathlib import Path
import httpx

OUTPUT_DIR = Path("probe_output")
OUTPUT_DIR.mkdir(exist_ok=True)

BASE = "https://m.21cineplex.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Referer": "https://m.21cineplex.com/",
    "Origin":  "https://m.21cineplex.com",
    "Accept":  "application/json, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
}


def save(name: str, data) -> None:
    path = OUTPUT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  → Disimpan: {path}")


def preview(data, label: str = "", max_items: int = 3) -> None:
    """Print ringkasan struktur data."""
    if isinstance(data, list):
        print(f"  {label} → list [{len(data)} items]")
        for item in data[:max_items]:
            if isinstance(item, dict):
                print(f"    keys: {list(item.keys())}")
                for k, v in item.items():
                    print(f"      {k}: {str(v)[:80]!r}")
            else:
                print(f"    {str(item)[:80]!r}")
        if len(data) > max_items:
            print(f"    ... (+{len(data) - max_items} more)")
    elif isinstance(data, dict):
        print(f"  {label} → dict keys: {list(data.keys())}")
        for k, v in data.items():
            if isinstance(v, list):
                print(f"    {k}: list[{len(v)}]")
                for item in v[:2]:
                    print(f"      {str(item)[:100]!r}")
            else:
                print(f"    {k}: {str(v)[:100]!r}")
    else:
        print(f"  {label} → {str(data)[:200]!r}")


async def probe():
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:

        # ── 1. City List ──────────────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 1 ] POST /api/theater?type=getCityList")
        print("="*60)
        try:
            r = await client.post(f"{BASE}/api/theater?type=getCityList")
            print(f"  status: {r.status_code}  content-type: {r.headers.get('content-type','?')}")
            print(f"  body preview: {r.text[:300]!r}")
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                data = r.json()
                save("01_city_list", data)
                preview(data, "city_list")
        except Exception as e:
            print(f"  ERROR: {e}")

        # ── 2. Theater List per City (coba beberapa city_id) ──────────────────
        print("\n" + "="*60)
        print("[ 2 ] POST /api/theater?type=getTheaterList  (per city)")
        print("="*60)

        # Kita coba city_id dari XHR yang tertangkap (72) + beberapa lain
        test_city_ids = [72, 1, 2, 12]
        for city_id in test_city_ids:
            try:
                # Coba variasi payload
                for payload in [
                    {"city_id": city_id},
                    {"cityId": city_id},
                    f"city_id={city_id}",
                ]:
                    if isinstance(payload, str):
                        r = await client.post(
                            f"{BASE}/api/theater?type=getTheaterList",
                            content=payload,
                            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                        )
                    else:
                        r = await client.post(
                            f"{BASE}/api/theater?type=getTheaterList",
                            json=payload,
                        )
                    print(f"  city_id={city_id} payload={payload!r}: "
                          f"status={r.status_code} len={len(r.text)}")
                    if r.status_code == 200 and len(r.text) > 50:
                        print(f"  body: {r.text[:400]!r}")
                        try:
                            data = r.json()
                            save(f"02_theaters_city{city_id}", data)
                            preview(data, f"theaters city={city_id}")
                        except Exception:
                            pass
                        break
            except Exception as e:
                print(f"  ERROR city={city_id}: {e}")

        # ── 3. Now Playing per City ───────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 3 ] GET /api/movies?type=now-playing&city_id=72")
        print("="*60)
        try:
            r = await client.get(f"{BASE}/api/movies?type=now-playing&city_id=72")
            print(f"  status: {r.status_code}  len: {len(r.text)}")
            print(f"  body preview: {r.text[:400]!r}")
            if r.status_code == 200:
                try:
                    data = r.json()
                    save("03_movies_now_playing_city72", data)
                    preview(data, "now_playing city=72")
                except Exception as e:
                    print(f"  JSON parse error: {e}")
        except Exception as e:
            print(f"  ERROR: {e}")

        # ── 4. Coba city_id lain untuk now-playing ────────────────────────────
        print("\n" + "="*60)
        print("[ 4 ] GET /api/movies?type=now-playing  (variasi city_id)")
        print("="*60)
        for cid in [1, 2, 12, 0]:
            try:
                r = await client.get(f"{BASE}/api/movies?type=now-playing&city_id={cid}")
                print(f"  city_id={cid}: status={r.status_code} len={len(r.text)}")
                if r.status_code == 200 and len(r.text) > 100:
                    try:
                        data = r.json()
                        save(f"04_movies_nowplaying_city{cid}", data)
                        preview(data, f"now_playing city={cid}")
                    except Exception:
                        print(f"    body: {r.text[:200]!r}")
            except Exception as e:
                print(f"  city_id={cid}: ERROR {e}")

        # ── 5. Schedule per Theater ───────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 5 ] Schedule per theater — coba berbagai endpoint & format")
        print("="*60)

        # Kita belum tahu theater_id formatnya — coba beberapa kemungkinan
        test_theater_ids = ["SBYCIWO", "BDGPVJ", "122", "2", "JKTBLM"]
        schedule_endpoints = [
            "/api/theater?type=getSchedule&theater_id={tid}",
            "/api/schedule?theater_id={tid}",
            "/api/theater/schedule?id={tid}",
            "/api/theater?type=getSchedule&cinema_id={tid}",
        ]
        for tid in test_theater_ids[:2]:  # cukup 2 dulu untuk hemat waktu
            for ep_template in schedule_endpoints:
                ep = ep_template.format(tid=tid)
                try:
                    r = await client.get(f"{BASE}{ep}")
                    print(f"  tid={tid} {ep}: status={r.status_code} len={len(r.text)}")
                    if r.status_code == 200 and len(r.text) > 100:
                        print(f"    body: {r.text[:300]!r}")
                        try:
                            data = r.json()
                            save(f"05_schedule_{tid}", data)
                            preview(data, f"schedule tid={tid}")
                        except Exception:
                            pass
                except Exception as e:
                    print(f"  ERROR: {e}")

        # ── 6. Cinemas endpoint ───────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 6 ] GET /cinemas  (Next.js RSC route yang terdeteksi)")
        print("="*60)
        rsc_headers = {
            **HEADERS,
            "RSC": "1",
            "Next-Router-State-Tree": "%5B%22%22%2C%7B%7D%5D",
            "Accept": "text/x-component",
        }
        for url in [
            f"{BASE}/cinemas?_rsc=18tj9",
            f"{BASE}/cinemas",
        ]:
            try:
                r = await client.get(url, headers=rsc_headers)
                print(f"  {url}: status={r.status_code} len={len(r.text)}")
                print(f"  content-type: {r.headers.get('content-type','?')}")
                print(f"  body: {r.text[:400]!r}")
                save("06_cinemas_rsc", {"url": url, "body": r.text[:5000]})
            except Exception as e:
                print(f"  ERROR: {e}")

        # ── 7. Auth session (perlu token?) ────────────────────────────────────
        print("\n" + "="*60)
        print("[ 7 ] GET /api/auth/session  (cek apakah perlu auth)")
        print("="*60)
        try:
            r = await client.get(f"{BASE}/api/auth/session")
            print(f"  status: {r.status_code}")
            print(f"  body: {r.text[:300]!r}")
            if r.status_code == 200:
                try:
                    data = r.json()
                    save("07_auth_session", data)
                    preview(data, "auth_session")
                except Exception:
                    pass
        except Exception as e:
            print(f"  ERROR: {e}")

        # ── Ringkasan ─────────────────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ SELESAI ] File yang dihasilkan:")
        print("="*60)
        for f in sorted(OUTPUT_DIR.iterdir()):
            print(f"  {f.name:45s} {f.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    asyncio.run(probe())