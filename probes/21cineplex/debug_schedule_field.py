"""
debug_schedule_field.py
Cek struktur field 'schedule' di response dc21-api.
Jalankan: python debug_schedule_field.py
"""
import asyncio
import json
import httpx

DC21_API    = "https://dc21-api.21cineplex.com"
MOBILE_BASE = "https://m.21cineplex.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Referer": f"{MOBILE_BASE}/",
    "Origin":  MOBILE_BASE,
    "Accept":  "application/json, */*",
}

async def main():
    async with httpx.AsyncClient(timeout=20, verify=False) as client:
        resp = await client.get(
            f"{DC21_API}/cinema/schedule/theater",
            headers=HEADERS,
            params={"cinema_id": "JKTBLOM"},
        )
        data = resp.json()
        days = data.get("data", {}).get("value") or []

        for day in days[:1]:
            films = (day.get("cinema") or {}).get("xxi") or []
            for film in films[:3]:
                print(f"title:    {film.get('title')!r}")
                print(f"schedule: {json.dumps(film.get('schedule'), ensure_ascii=False, indent=2)}")
                print()

asyncio.run(main())