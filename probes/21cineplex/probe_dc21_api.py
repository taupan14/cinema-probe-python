"""
probe_dc21_api.py
=================
Probe endpoint dc21-api.21cineplex.com yang ditemukan di response JSON.
Ini adalah API backend utama yang dipakai mobile app XXI.

Jalankan: python probe_dc21_api.py

Yang ingin kita temukan:
1. Endpoint untuk dapat semua city_id yang valid
2. Endpoint schedule/theater — dapat list theater + film yang tayang per theater
"""

import asyncio
import json
from pathlib import Path
import httpx

OUTPUT_DIR = Path("probe_output")
OUTPUT_DIR.mkdir(exist_ok=True)

DC21 = "https://dc21-api.21cineplex.com"
MOBILE = "https://m.21cineplex.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Referer": "https://m.21cineplex.com/",
    "Origin":  "https://m.21cineplex.com",
    "Accept":  "application/json, */*",
}


def save(name, data):
    path = OUTPUT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"     → Disimpan: {path}  ({path.stat().st_size:,} bytes)")


def preview(data, depth=0, max_depth=5, max_list=2):
    indent = "  " * depth
    if depth > max_depth:
        return
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                print(f"{indent}{k}:")
                preview(v, depth+1, max_depth, max_list)
            else:
                print(f"{indent}{k}: {str(v)[:120]!r}")
    elif isinstance(data, list):
        print(f"{indent}[list: {len(data)} items]")
        for i, item in enumerate(data[:max_list]):
            print(f"{indent}  [{i}]:")
            preview(item, depth+2, max_depth, max_list)
        if len(data) > max_list:
            print(f"{indent}  ... (+{len(data)-max_list} more)")


async def probe():
    async with httpx.AsyncClient(
        headers=HEADERS, timeout=20, follow_redirects=True
    ) as client:

        # ── 1. City list dari dc21-api ────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 1 ] City list — coba berbagai endpoint")
        print("="*60)
        city_endpoints = [
            f"{DC21}/cinema/city",
            f"{DC21}/cinema/cities",
            f"{DC21}/city",
            f"{MOBILE}/api/theater?type=getCityList",
            f"{MOBILE}/api/city",
        ]
        for url in city_endpoints:
            try:
                r = await client.get(url)
                print(f"  {url}")
                print(f"  status={r.status_code} len={len(r.text)} ct={r.headers.get('content-type','?')[:40]}")
                if r.status_code == 200 and len(r.text) > 10:
                    print(f"  body: {r.text[:300]!r}")
                    try:
                        data = r.json()
                        name = url.split("/")[-1].replace("?", "_")
                        save(f"city_{name}", data)
                        preview(data)
                    except Exception:
                        pass
                print()
            except Exception as e:
                print(f"  ERROR: {e}\n")

        # ── 2. Schedule by theater ────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 2 ] dc21-api /cinema/schedule/theater")
        print("="*60)

        # Kita belum punya theater_id yang valid untuk dc21-api.
        # Coba: tanpa param, dengan berbagai format theater_id
        schedule_theater_variants = [
            {},                                      # tanpa param → mungkin return semua
            {"theater_id": "BDGPVJ"},               # kode alfanumerik lama
            {"theater_id": "2"},                    # numeric
            {"cinema_id": "BDGPVJ"},
            {"city_id": "2"},                       # by city
            {"city_id": 2},
        ]
        for params in schedule_theater_variants:
            try:
                r = await client.get(
                    f"{DC21}/cinema/schedule/theater", params=params
                )
                print(f"  params={params}  status={r.status_code} len={len(r.text)}")
                if r.status_code == 200 and len(r.text) > 20:
                    print(f"  body: {r.text[:400]!r}")
                    try:
                        data = r.json()
                        label = str(params).replace(" ", "").replace("'", "")[:30]
                        save(f"sched_theater_{label}", data)
                        preview(data)
                    except Exception:
                        pass
                print()
            except Exception as e:
                print(f"  ERROR: {e}\n")
            await asyncio.sleep(0.5)

        # ── 3. Schedule by movie ──────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 3 ] dc21-api /cinema/schedule/movie")
        print("="*60)
        # Kita punya movie_id dari JSON sebelumnya: 25MKT2, 16GITC, dll
        for params in [
            {"movie_id": "25MKT2"},
            {"movie_id": "25MKT2", "city_id": 2},
            {"parent_movie_id": "25MKT2"},
        ]:
            try:
                r = await client.get(
                    f"{DC21}/cinema/schedule/movie", params=params
                )
                print(f"  params={params}  status={r.status_code} len={len(r.text)}")
                if r.status_code == 200 and len(r.text) > 20:
                    print(f"  body: {r.text[:500]!r}")
                    try:
                        data = r.json()
                        label = str(params).replace(" ", "").replace("'", "")[:30]
                        save(f"sched_movie_{label}", data)
                        preview(data)
                    except Exception:
                        pass
                print()
            except Exception as e:
                print(f"  ERROR: {e}\n")
            await asyncio.sleep(0.5)

        # ── 4. Coba root dc21-api ─────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 4 ] Eksplorasi root dc21-api")
        print("="*60)
        for path in [
            "/", "/cinema", "/cinema/theater", "/theater",
            "/cinema/now-playing", "/cinema/schedule",
        ]:
            try:
                r = await client.get(f"{DC21}{path}")
                print(f"  {path}: status={r.status_code} len={len(r.text)}")
                if len(r.text) > 5 and len(r.text) < 2000:
                    print(f"  body: {r.text[:300]!r}")
            except Exception as e:
                print(f"  {path}: ERROR {e}")

        # ── 5. Coba apps-api ──────────────────────────────────────────────────
        print("\n" + "="*60)
        print("[ 5 ] apps-api.21cineplex.com — endpoint yang ditemukan")
        print("="*60)
        apps_endpoints = [
            "https://apps-api.21cineplex.com/mtix/banner/carousel",
        ]
        for url in apps_endpoints:
            try:
                r = await client.get(url)
                print(f"  {url}")
                print(f"  status={r.status_code} len={len(r.text)}")
                if r.status_code == 200 and len(r.text) > 10:
                    print(f"  body: {r.text[:200]!r}")
            except Exception as e:
                print(f"  ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(probe())