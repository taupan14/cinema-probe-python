"""
debug_showtime.py
Cek apakah field show_time ada di response dc21-api schedule.
Jalankan: python debug_showtime.py
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

# Cinema yang terbukti punya data dari probe sebelumnya
TEST_CINEMAS = [
    ("JKTBLOM", "BLOK M XXI"),
    ("JKTBASS", "BASSURA XXI"),
    ("JKTGAND", "GANDARIA CITY XXI"),
]

async def main():
    async with httpx.AsyncClient(timeout=20, verify=False) as client:
        for cinema_id, name in TEST_CINEMAS:
            print(f"\n{'='*60}")
            print(f"[ {name} — {cinema_id} ]")
            print(f"{'='*60}")

            # ── dc21-api ──────────────────────────────────────────────────
            resp = await client.get(
                f"{DC21_API}/cinema/schedule/theater",
                headers=HEADERS,
                params={"cinema_id": cinema_id},
            )
            print(f"dc21-api: status={resp.status_code} len={len(resp.text)}")

            if resp.status_code == 200:
                data = resp.json()
                days = data.get("data", {}).get("value") or []
                print(f"  days: {len(days)}")
                for day in days[:1]:
                    print(f"  date: {day.get('date')}")
                    cinema_subs = day.get("cinema") or {}
                    for sub_key, films in cinema_subs.items():
                        print(f"  sub_key: {sub_key!r} → {len(films) if films else 0} films")
                        for film in (films or [])[:2]:
                            print(f"    title:     {film.get('title')!r}")
                            print(f"    show_time: {film.get('show_time')!r}")
                            print(f"    showtime:  {film.get('showtime')!r}")
                            # Print semua key yang ada
                            print(f"    ALL KEYS:  {sorted(film.keys())}")
                            print()

            # ── mobile fallback ───────────────────────────────────────────
            print(f"\nmobile getTheaterSchedule:")
            resp2 = await client.post(
                f"{MOBILE_BASE}/api/theater?type=getTheaterSchedule",
                headers={**HEADERS, "Content-Type": "application/json"},
                json={"cinema_id": cinema_id},
            )
            print(f"  status={resp2.status_code} len={len(resp2.text)}")
            if resp2.status_code == 200:
                data2 = resp2.json()
                days2 = data2.get("data", {}).get("value") or []
                print(f"  days: {len(days2)}")
                for day in days2[:1]:
                    print(f"  date: {day.get('date')}")
                    cinema_subs = day.get("cinema") or {}
                    for sub_key, films in cinema_subs.items():
                        print(f"  sub_key: {sub_key!r} → {len(films) if films else 0} films")
                        for film in (films or [])[:2]:
                            print(f"    title:     {film.get('title')!r}")
                            print(f"    show_time: {film.get('show_time')!r}")
                            print(f"    showtime:  {film.get('showtime')!r}")
                            print(f"    ALL KEYS:  {sorted(film.keys())}")
                            print()

            await asyncio.sleep(0.5)

asyncio.run(main())