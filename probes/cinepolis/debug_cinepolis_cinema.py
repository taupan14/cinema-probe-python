"""
debug_cinepolis_cinema.py
Inspect struktur HTML cinema-page.aspx untuk fix selector.
Jalankan: python debug_cinepolis_cinema.py
"""
import asyncio
import re
from bs4 import BeautifulSoup
import httpx

BASE = "https://cinepolis.co.id"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
}

async def main():
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(f"{BASE}/cinema-page.aspx", headers=HEADERS)
        print(f"status={resp.status_code} len={len(resp.text)}")

        soup = BeautifulSoup(resp.text, "lxml")

        # 1. Lihat semua elemen dengan onclick yang mengandung kata cinema/Cinema
        print("\n[ onclick patterns mengandung 'cinema'/'Cinema'/'Theater' ]")
        for el in soup.select("[onclick]"):
            oc = el.get("onclick", "")
            if any(k in oc for k in ["cinema", "Cinema", "Theater", "theater", "SelCin"]):
                print(f"  tag={el.name} text={el.get_text(strip=True)[:40]!r}")
                print(f"  onclick={oc[:150]!r}")
                print()

        # 2. Semua onclick patterns unik
        print("\n[ Semua onclick patterns unik (50 pertama) ]")
        patterns = set()
        for el in soup.select("[onclick]"):
            oc = el.get("onclick", "").strip()[:80]
            patterns.add(oc)
        for p in sorted(patterns)[:50]:
            print(f"  {p!r}")

        # 3. Preview HTML mentah 2000 char pertama
        print(f"\n[ HTML preview 2000 chars ]")
        print(resp.text[:2000])

asyncio.run(main())