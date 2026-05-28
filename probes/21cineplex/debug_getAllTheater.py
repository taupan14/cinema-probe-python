"""
debug_getAllTheater.py
Cek raw response dari getAllTheater untuk diagnosa TypeError NoneType.
Jalankan: python debug_getAllTheater.py
"""
import asyncio
import json
import httpx

MOBILE_BASE = "https://m.21cineplex.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Referer":         f"{MOBILE_BASE}/",
    "Origin":          MOBILE_BASE,
    "Accept":          "application/json, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
    "Content-Type":    "application/json",
}

async def main():
    async with httpx.AsyncClient(timeout=20, verify=False) as client:

        # ── 1. Cek raw response getAllTheater ──────────────────────────────
        print("=" * 60)
        print("[ 1 ] Raw response getAllTheater city_id=2 (Bandung)")
        print("=" * 60)
        resp = await client.post(
            f"{MOBILE_BASE}/api/theater?type=getAllTheater",
            headers=HEADERS,
            json={"city_id": 2},
        )
        print(f"status: {resp.status_code}")
        print(f"len:    {len(resp.text)}")
        print(f"body:   {resp.text!r}")
        print()

        # ── 2. Coba tanpa body ─────────────────────────────────────────────
        print("=" * 60)
        print("[ 2 ] getAllTheater tanpa body (kosong)")
        print("=" * 60)
        resp2 = await client.post(
            f"{MOBILE_BASE}/api/theater?type=getAllTheater",
            headers=HEADERS,
        )
        print(f"status: {resp2.status_code}")
        print(f"len:    {len(resp2.text)}")
        print(f"body:   {resp2.text!r}")
        print()

        # ── 3. Coba dengan city_id sebagai string ──────────────────────────
        print("=" * 60)
        print('[ 3 ] getAllTheater city_id="2" (string)')
        print("=" * 60)
        resp3 = await client.post(
            f"{MOBILE_BASE}/api/theater?type=getAllTheater",
            headers=HEADERS,
            json={"city_id": "2"},
        )
        print(f"status: {resp3.status_code}")
        print(f"len:    {len(resp3.text)}")
        print(f"body:   {resp3.text!r}")
        print()

        # ── 4. Coba getAllTheater sebagai GET ──────────────────────────────
        print("=" * 60)
        print("[ 4 ] getAllTheater via GET dengan query param")
        print("=" * 60)
        resp4 = await client.get(
            f"{MOBILE_BASE}/api/theater",
            headers=HEADERS,
            params={"type": "getAllTheater", "city_id": 2},
        )
        print(f"status: {resp4.status_code}")
        print(f"len:    {len(resp4.text)}")
        print(f"body:   {resp4.text[:300]!r}")
        print()

        # ── 5. Coba getAllTheater dari hasil intercept Playwright ───────────
        # Waktu probe, getAllTheater dipanggil TANPA city_id filter
        # (untuk menampilkan semua theater dalam radius user)
        # Mungkin endpoint ini butuh koordinat, bukan city_id
        print("=" * 60)
        print("[ 5 ] getAllTheater dengan lat/lng (koordinat Jakarta)")
        print("=" * 60)
        resp5 = await client.post(
            f"{MOBILE_BASE}/api/theater?type=getAllTheater",
            headers=HEADERS,
            json={
                "city_id":   2,
                "latitude":  -6.9175,
                "longitude": 107.6191,
            },
        )
        print(f"status: {resp5.status_code}")
        print(f"len:    {len(resp5.text)}")
        print(f"body:   {resp5.text[:300]!r}")
        print()

        # ── 6. Coba payload persis seperti yang dikirim browser ────────────
        # Dari probe, app kirim dua request getAllTheater:
        # - Pertama: tanpa city filter (semua theater)
        # - Kedua: dengan city filter setelah user pilih kota
        print("=" * 60)
        print("[ 6 ] getAllTheater persis seperti Playwright probe (tanpa filter)")
        print("=" * 60)
        resp6 = await client.post(
            f"{MOBILE_BASE}/api/theater?type=getAllTheater",
            headers={
                **HEADERS,
                # Tambah cookie yang ditemukan dari probe
                "Cookie": "mtix-city_id=72; NEXT_LOCALE=en",
            },
            json={},
        )
        print(f"status: {resp6.status_code}")
        print(f"len:    {len(resp6.text)}")
        print(f"body:   {resp6.text[:500]!r}")

asyncio.run(main())