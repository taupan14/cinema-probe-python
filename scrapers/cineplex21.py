"""
scrapers/cineplex21.py

PERUBAHAN v6 — Fix root cause: getAllTheater return value=null tanpa session browser.

Strategi baru untuk scrape_cinemas():
  getAllTheater butuh session cookie dari browser (Playwright) — tidak bisa
  dipanggil langsung via httpx. Ganti dengan dua pendekatan:

  Opsi A (UTAMA): POST /api/theater?type=getAllTheater via Playwright
    → Buka halaman /cinemas, intercept response getAllTheater yang dipanggil app
    → Hasilnya: list semua theater lengkap dengan cinema_id, nama, alamat

  Opsi B (FALLBACK): Parse link /cinemas/{cinema_id} dari halaman /cinemas
    → Sudah terbukti dari probe: 47+ link theater ditemukan di halaman tersebut
    → Tidak butuh API call, cukup scrape HTML yang sudah di-render Playwright

scrape_movies() tidak berubah — dc21-api schedule sudah terbukti bekerja.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from models import Cinema, CinemaMovie, Showtime
from utils import (
    AsyncHTTPClient, clean_text, normalize_city,
    normalize_format, normalize_age_rating,
    extract_duration, today_iso,
)
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

MOBILE_BASE = "https://m.21cineplex.com"
DC21_API    = "https://dc21-api.21cineplex.com"

MOBILE_HEADERS = {
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


class Cineplex21Scraper(BaseScraper):
    SOURCE   = "21cineplex"
    CHAIN    = "XXI"
    BASE_URL = MOBILE_BASE

    def __init__(self, **kwargs):
        super().__init__(concurrency=5, **kwargs)
        self._detail_cache: dict[str, dict[str, dict]] = {}
        self._cache_lock = asyncio.Lock()
        # city_map dari getCityList intercept — disimpan agar scrape_movies() bisa pakai
        self._city_map_cache: dict[str, int] = {}

    # =========================================================================
    # SCRAPE CINEMAS — Playwright (wajib, karena getAllTheater butuh session)
    # =========================================================================

    async def scrape_cinemas(self, client: AsyncHTTPClient) -> list[Cinema]:
        """
        Strategi baru (v7) — sitemap.xml sebagai sumber daftar cinema:

        Root cause lama: getAllTheater API adalah location-based (return ~101
        bioskop terdekat dari IP server = Jabodetabek), bukan city-based.
        Tidak ada city_id parameter yang mengubah hasilnya.

        Solusi:
          1. Fetch sitemap.xml → ekstrak 267 cinema_id (semua XXI Indonesia)
          2. Buka 1 halaman /cinemas via Playwright untuk dapat:
             - getCityList → city_map (city_name → city_id)
             - getAllTheater → metadata (nama, koordinat, alamat) untuk ~101 cinema
          3. Untuk cinema_id yang ada di sitemap tapi tidak ada di getAllTheater,
             fetch detail via /cinemas/{cinema_id} (intercept getTheaterSchedule
             response yang mengandung cafe_group = canonical cinema_id).
          4. Build Cinema object untuk semua 267 cinema_id.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.logger.error(
                "playwright tidak terinstall. Jalankan: "
                "pip install playwright && playwright install chromium"
            )
            return []

        # ── STEP 1: Fetch sitemap.xml → semua cinema_id ───────────────────
        self.logger.info("XXI: fetching sitemap.xml ...")
        sitemap_ids: list[str] = []
        try:
            resp = await client.get(
                f"{MOBILE_BASE}/sitemap.xml",
                headers={"User-Agent": MOBILE_HEADERS["User-Agent"]},
            )
            if resp.status_code == 200:
                sitemap_ids = list(dict.fromkeys(
                    re.findall(r'/cinemas/([A-Z]{3}[A-Z0-9]{3,})', resp.text)
                ))
                self.logger.info(f"XXI: {len(sitemap_ids)} cinema_id dari sitemap.xml")
        except Exception as e:
            self.logger.warning(f"XXI: sitemap.xml fetch error: {e}")

        if not sitemap_ids:
            self.logger.error("XXI: sitemap.xml kosong — scraping dihentikan")
            return []

        # ── STEP 2: Playwright — getCityList + getAllTheater metadata ──────
        city_map:     dict[str, int]  = {}   # "JAKARTA" → 10
        meta_map:     dict[str, dict] = {}   # cinema_id → {name, address, lat, lng}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=MOBILE_HEADERS["User-Agent"],
                viewport={"width": 390, "height": 844},
            )
            page = await context.new_page()

            async def on_response(resp):
                url = resp.url
                if "getCityList" in url:
                    try:
                        data   = json.loads(await resp.text())
                        cities = (data.get("data") or {}).get("value") or []
                        for c in cities:
                            name = (c.get("city_name") or "").upper()
                            cid  = c.get("city_id") or 0
                            if name and cid:
                                city_map[name] = cid
                    except Exception:
                        pass

                if "getAllTheater" in url:
                    try:
                        data  = json.loads(await resp.text())
                        value = (data.get("data") or {}).get("value") or []
                        for t in (value or []):
                            subtypes = t.get("theater_subtype") or {}
                            for sub_key, sub in subtypes.items():
                                if not isinstance(sub, dict):
                                    continue
                                cid = sub.get("cinema_id")
                                if cid and cid not in meta_map:
                                    coord = sub.get("coordinate") or ""
                                    lat, lng = 0.0, 0.0
                                    parts = coord.split(",")
                                    if len(parts) == 2:
                                        try:
                                            lat = float(parts[0].strip())
                                            lng = float(parts[1].strip())
                                        except ValueError:
                                            pass
                                    meta_map[cid] = {
                                        "name":    clean_text(sub.get("cinema_name") or t.get("theater_name") or ""),
                                        "address": clean_text(sub.get("cinema_address") or ""),
                                        "lat":     lat,
                                        "lng":     lng,
                                    }
                    except Exception:
                        pass

            page.on("response", on_response)
            self.logger.info("XXI: Playwright membuka /cinemas ...")
            try:
                await page.goto(f"{MOBILE_BASE}/cinemas", wait_until="networkidle", timeout=30_000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                self.logger.warning(f"XXI: goto /cinemas: {e}")
            page.remove_listener("response", on_response)

            self.logger.info(
                f"XXI: city_map={len(city_map)} kota, "
                f"meta_map={len(meta_map)} cinema dari getAllTheater"
            )

            # ── STEP 3: Navigate semua cinema_id untuk dapat metadata + schedule
            # Root cause dari probe:
            #   - getTheaterSchedule HANYA bekerja via Playwright navigate (intercept)
            #   - POST via httpx selalu return 0 days (butuh session browser)
            #   - Nama, alamat, koordinat ada di JSON-LD di setiap /cinemas/{id}
            # Solusi: navigate semua 267 cinema, intercept getTheaterSchedule,
            # parse JSON-LD, simpan semua ke cache untuk dipakai scrape_movies.

            # Cache schedule per cinema_id — dipakai oleh scrape_movies()
            # supaya tidak perlu navigate ulang
            self._schedule_cache: dict[str, list[dict]] = {}

            # Navigate semua sitemap_ids (bukan hanya yang missing dari getAllTheater)
            # karena meta_map dari getAllTheater tidak punya alamat/koordinat.
            all_ids_to_visit = sitemap_ids  # semua 267
            self.logger.info(
                f"XXI: navigating {len(all_ids_to_visit)} cinema untuk "
                f"JSON-LD metadata + schedule intercept ..."
            )

            # Playwright single-page — navigate sequential dengan delay
            # (concurrent di satu page tidak aman; buka tab baru terlalu berat)
            visited = 0
            for cinema_id in all_ids_to_visit:
                schedule_buf: list[dict] = []

                async def cap_ld(resp, sb=schedule_buf):
                    if "getTheaterSchedule" in resp.url:
                        try:
                            data  = json.loads(await resp.text())
                            value = (data.get("data") or {}).get("value") or []
                            if isinstance(value, list):
                                sb.extend(value)
                        except Exception:
                            pass

                page.on("response", cap_ld)
                try:
                    await page.goto(
                        f"{MOBILE_BASE}/cinemas/{cinema_id}",
                        wait_until="networkidle",
                        timeout=15_000,
                    )
                    await page.wait_for_timeout(800)
                except Exception as e:
                    self.logger.debug(f"XXI goto /cinemas/{cinema_id}: {e}")
                finally:
                    page.remove_listener("response", cap_ld)

                # Simpan schedule ke cache
                if schedule_buf:
                    self._schedule_cache[cinema_id] = schedule_buf

                # Parse JSON-LD untuk nama, alamat, koordinat
                try:
                    json_ld_raw = await page.evaluate("""() => {
                        const scripts = document.querySelectorAll(
                            'script[type="application/ld+json"]'
                        );
                        for (const s of scripts) {
                            try {
                                const d = JSON.parse(s.textContent);
                                if (d['@type'] === 'MovieTheater') return d;
                            } catch(e) {}
                        }
                        return null;
                    }""")
                except Exception:
                    json_ld_raw = None

                name, address, lat, lng = "", "", 0.0, 0.0
                if json_ld_raw:
                    name    = clean_text(json_ld_raw.get("name") or "")
                    addr_obj = json_ld_raw.get("address") or {}
                    address = clean_text(
                        addr_obj.get("streetAddress") or ""
                        if isinstance(addr_obj, dict) else str(addr_obj)
                    )
                    geo = json_ld_raw.get("geo") or {}
                    try:
                        lat = float(geo.get("latitude") or 0)
                        lng = float(geo.get("longitude") or 0)
                    except (ValueError, TypeError):
                        pass

                # Fallback nama dari page title jika JSON-LD tidak ada
                if not name:
                    try:
                        title_str = await page.title()
                        if " | " in title_str:
                            name = clean_text(title_str.split(" | ")[0])
                    except Exception:
                        pass

                # Update meta_map — override apapun yang ada sebelumnya
                # karena JSON-LD lebih akurat dari getAllTheater
                meta_map[cinema_id] = {
                    "name":    name or meta_map.get(cinema_id, {}).get("name") or cinema_id,
                    "address": address,
                    "lat":     lat,
                    "lng":     lng,
                }

                visited += 1
                if visited % 20 == 0:
                    self.logger.info(
                        f"XXI: metadata+schedule {visited}/{len(all_ids_to_visit)} "
                        f"(schedule_cache={len(self._schedule_cache)})"
                    )

                await asyncio.sleep(self.delay * 0.5)  # lebih ringan dari delay penuh

            await browser.close()

        # Simpan city_map
        self._city_map_cache = city_map

        # ── STEP 4: Build Cinema objects dari semua sitemap_ids ───────────
        cinemas: list[Cinema] = []
        for cinema_id in sitemap_ids:
            meta = meta_map.get(cinema_id) or {}
            name = meta.get("name") or cinema_id   # fallback ke ID kalau nama tidak dapat

            city_str = self._city_from_cinema_id(cinema_id)

            # Lookup city_id case-insensitive
            city_id_int = 0
            city_upper  = city_str.upper()
            for map_name, map_id in city_map.items():
                if city_upper in map_name or map_name in city_upper:
                    city_id_int = map_id
                    break
            city_id_str = str(city_id_int) if city_id_int else ""

            cinemas.append(Cinema(
                name        = name,
                chain       = self.CHAIN,
                city        = normalize_city(city_str),
                address     = meta.get("address") or "",
                source      = self.SOURCE,
                external_id = cinema_id,
                lat         = meta.get("lat") or 0.0,
                lng         = meta.get("lng") or 0.0,
                booking_url = (
                    f"{MOBILE_BASE}/cinemas/{cinema_id}"
                    + (f"?city_id={city_id_str}" if city_id_str else "")
                ),
            ))

        self.logger.info(f"XXI: total {len(cinemas)} cinema dari sitemap")
        return cinemas

    def _parse_theater_data(
        self, theater_data: list[dict], city_map: dict[str, int]
    ) -> list[Cinema]:
        """
        Parse response getAllTheater yang diintercept.

        Format tiap item:
        {
          "theater_name": "AEON MALL TANJUNG BARAT XXI",
          "theater_subtype": {
            "xxi": {
              "cinema_id":      "JKTAETB",
              "cinema_name":    "AEON MALL TANJUNG BARAT XXI",
              "cinema_address": "...",
              "coordinate":     "-6.30697,106.83993",
              "is_mtix":        2,
            }
          }
        }
        """
        cinemas: list[Cinema] = []
        seen: set[str] = set()

        for t in theater_data:
            theater_name = clean_text(t.get("theater_name") or "")
            subtypes     = t.get("theater_subtype") or {}

            for sub_key, sub_data in subtypes.items():
                if not isinstance(sub_data, dict):
                    continue

                cinema_id   = clean_text(sub_data.get("cinema_id") or "")
                cinema_name = clean_text(sub_data.get("cinema_name") or theater_name)
                if not cinema_id or not cinema_name or cinema_id in seen:
                    continue
                seen.add(cinema_id)

                # Parse koordinat "lat,lng"
                lat, lng = 0.0, 0.0
                coord = sub_data.get("coordinate") or ""
                if coord:
                    parts = coord.split(",")
                    if len(parts) == 2:
                        try:
                            lat = float(parts[0].strip())
                            lng = float(parts[1].strip())
                        except ValueError:
                            pass

                # Cari city dari nama cinema (prefix 3 huruf = kode kota)
                # e.g. "JKTAETB" → "JKT" → Jakarta
                city = self._city_from_cinema_id(cinema_id)

                # Tidak ada filter kota di sini — theater yang masuk ke sini
                # sudah berasal dari response getAllTheater untuk kota target.
                # Filter hanya dilakukan di _fetch_theaters_per_city (level city_id).

                # Case-insensitive city_id lookup
                city_id = 0
                city_upper = city.upper()
                for map_name, map_id in city_map.items():
                    if city_upper in map_name.upper() or map_name.upper() in city_upper:
                        city_id = map_id
                        break

                city_id_str = str(city_id) if city_id else ""

                cinemas.append(Cinema(
                    name        = cinema_name,
                    chain       = self.CHAIN,
                    city        = normalize_city(city),
                    address     = clean_text(sub_data.get("cinema_address") or ""),
                    source      = self.SOURCE,
                    external_id = cinema_id,
                    lat         = lat,
                    lng         = lng,
                    booking_url = (
                        f"{MOBILE_BASE}/cinemas/{cinema_id}"
                        + (f"?city_id={city_id_str}" if city_id_str else "")
                    ),
                ))

        return cinemas

    async def _parse_cinemas_from_page(
        self, page, city_map: dict[str, int]
    ) -> list[Cinema]:
        """
        Fallback: parse link /cinemas/{cinema_id} dari HTML.
        Terbukti dari probe: 47 link ditemukan di halaman /cinemas.
        """
        cinemas: list[Cinema] = []
        seen: set[str] = set()
        try:
            links = await page.eval_on_selector_all(
                "a[href*='/cinemas/']",
                "els => els.map(e => ({href: e.href, text: e.innerText.trim()}))",
            )
            self.logger.info(f"HTML fallback: {len(links)} link /cinemas/ ditemukan")
            for link in links:
                href = link.get("href", "")
                name = clean_text(link.get("text", ""))
                m    = re.search(r'/cinemas/([A-Z0-9]{4,10})$', href)
                if not m or not name:
                    continue
                cinema_id = m.group(1)
                if cinema_id in seen:
                    continue
                seen.add(cinema_id)
                city = self._city_from_cinema_id(cinema_id)

                # Case-insensitive city_id lookup (tidak filter kota di sini)
                city_id = 0
                city_upper = city.upper()
                for map_name, map_id in city_map.items():
                    if city_upper in map_name.upper() or map_name.upper() in city_upper:
                        city_id = map_id
                        break
                city_id_str = str(city_id) if city_id else ""

                cinemas.append(Cinema(
                    name        = name,
                    chain       = self.CHAIN,
                    city        = normalize_city(city),
                    source      = self.SOURCE,
                    external_id = cinema_id,
                    booking_url = (
                        f"{MOBILE_BASE}/cinemas/{cinema_id}"
                        + (f"?city_id={city_id_str}" if city_id_str else "")
                    ),
                ))
        except Exception as e:
            self.logger.warning(f"HTML fallback: {e}")
        return cinemas

    # Fase awal: 20 kota prioritas. Matching dilakukan case-insensitive dan
    # substring agar tetap cocok walau API mengembalikan nama kota yang sedikit
    # berbeda (mis. "Dki Jakarta", "Jakarta Selatan", "Kota Bekasi", dll.)
    TARGET_CITY_KEYWORDS: set[str] = {
        "jakarta", "bekasi", "surabaya", "bandung", "bogor",
        "tangerang", "medan", "makassar", "semarang", "depok",
        "palembang", "denpasar", "bali", "yogyakarta", "malang",
        "surakarta", "solo", "batam", "pekanbaru", "balikpapan",
    }

    def _is_target_city(self, city_name: str) -> bool:
        """
        Return True jika city_name mengandung salah satu keyword kota target.
        Case-insensitive dan substring match — aman untuk variasi nama API.
        Contoh: "Kota Bekasi", "DKI JAKARTA", "Kab. Bogor" semua akan match.
        """
        normalized = city_name.lower()
        return any(kw in normalized for kw in self.TARGET_CITY_KEYWORDS)

    async def _fetch_theaters_per_city(
        self, page, city_map: dict[str, int], existing: list[dict]
    ) -> list[Cinema]:
        """
        Iterasi kota-kota target via Playwright agar app memanggil getAllTheater
        per kota. Fix dari versi lama:
          - Tidak ada [:10] hard cap
          - city_map matching sekarang case-insensitive + substring
          - existing_ids tidak di-rebuild ulang (pakai seen_ids yang live-update)
          - all_theaters hanya berisi theater baru (tidak campur dengan existing)
        """
        # seen_ids: track semua cinema_id yang sudah diketahui (existing + baru)
        seen_ids: set[str] = {
            str(sub["cinema_id"])
            for t in existing
            for sub in (t.get("theater_subtype") or {}).values()
            if isinstance(sub, dict) and sub.get("cinema_id")
        }

        # all_new_theaters: hanya theater yang benar-benar baru
        all_new_theaters: list[dict] = []

        async def on_resp(resp):
            if "getAllTheater" not in resp.url:
                return
            try:
                body  = await resp.text()
                data  = json.loads(body)
                value = data.get("data", {}).get("value") or []
                for t in value:
                    subtypes = t.get("theater_subtype") or {}
                    has_new  = False
                    for sub in subtypes.values():
                        if not isinstance(sub, dict):
                            continue
                        cid = sub.get("cinema_id")
                        if cid and cid not in seen_ids:
                            seen_ids.add(cid)
                            has_new = True
                    if has_new:
                        all_new_theaters.append(t)
            except Exception:
                pass

        page.on("response", on_resp)

        # Ambil city_id untuk semua kota yang match TARGET_CITY_KEYWORDS
        # Matching case-insensitive — city_map key bisa uppercase atau mixed
        target_city_ids = [
            cid
            for name, cid in city_map.items()
            if cid and self._is_target_city(name)
        ]
        self.logger.info(
            f"XXI city iteration: {len(target_city_ids)} kota target ditemukan "
            f"dari {len(city_map)} kota di city_map"
        )
        if len(target_city_ids) == 0:
            self.logger.warning(
                f"XXI: 0 kota match TARGET_CITY_KEYWORDS! "
                f"Sample city_map keys: {list(city_map.keys())[:10]}"
            )

        for city_id in target_city_ids:
            try:
                await page.goto(
                    f"{MOBILE_BASE}/cinemas?city_id={city_id}",
                    wait_until="networkidle",
                    timeout=15_000,
                )
                await page.wait_for_timeout(1500)
            except Exception as e:
                self.logger.debug(f"city_id={city_id}: {e}")

        page.remove_listener("response", on_resp)

        self.logger.info(
            f"XXI city iteration selesai: +{len(all_new_theaters)} theater baru"
        )
        return self._parse_theater_data(all_new_theaters, city_map)

    @staticmethod
    def _city_from_cinema_id(cinema_id: str) -> str:
        """
        Ekstrak nama kota dari prefix cinema_id (3 huruf pertama).
        Contoh: JKTAETB → Jakarta, BDGPVJ → Bandung, SBYCWO → Surabaya

        Prefix map diperluas untuk semua kota operasional XXI Indonesia.
        Prefix yang tidak dikenali akan return string prefix mentah (3 huruf),
        sehingga city_map lookup di caller masih bisa mencoba mencocokkan.
        """
        prefix_map = {
            # Jawa
            "JKT": "Jakarta",      "BDG": "Bandung",    "SBY": "Surabaya",
            "SMG": "Semarang",     "YGY": "Yogyakarta", "MLG": "Malang",
            "BGR": "Bogor",        "BKS": "Bekasi",     "DPK": "Depok",
            "TGR": "Tangerang",    "TSL": "Tangerang Selatan",
            "CRB": "Cirebon",      "KDR": "Kediri",     "SLO": "Surakarta",
            "MJK": "Mojokerto",    "JMB": "Jember",     "MDR": "Madura",
            "PRW": "Purwokerto",   "TGL": "Tegal",       "MGL": "Magelang",
            "KLT": "Klaten",       "SRG": "Serang",      "CKR": "Cikarang",
            "CMH": "Cimahi",       "TSM": "Tasikmalaya", "SKB": "Sukabumi",
            "CJR": "Cianjur",      "PWK": "Purwakarta",  "KRW": "Karawang",
            "BYM": "Bayuwangi",
            # Sumatera
            "MDN": "Medan",        "PLM": "Palembang",   "PKU": "Pekanbaru",
            "BTM": "Batam",        "LPG": "Lampung",     "PDG": "Padang",
            "JMB": "Jambi",        "BKL": "Bengkulu",    "TJP": "Tanjung Pinang",
            "LHO": "Lhokseumawe", "BND": "Banda Aceh",  "SBG": "Sabang",
            "PRP": "Prapatan",     "SWT": "Sawit",
            # Kalimantan
            "BPN": "Balikpapan",   "SMD": "Samarinda",   "PNK": "Pontianak",
            "BJM": "Banjarmasin",  "PLK": "Palangka Raya","TRK": "Tarakan",
            "BLK": "Balikpapan",
            # Sulawesi
            "MKS": "Makassar",     "MNO": "Manado",      "PRU": "Palu",
            "KDR": "Kendari",      "GTL": "Gorontalo",
            # Bali & Nusa Tenggara
            "DPS": "Denpasar",     "MTR": "Mataram",     "KPG": "Kupang",
            "SBH": "Sumbawa",      "MBJ": "Labuan Bajo",
            # Maluku & Papua
            "AMB": "Ambon",        "JYP": "Jayapura",    "SRG": "Sorong",
            "MNK": "Manokwari",    "TRN": "Ternate",
        }
        prefix = cinema_id[:3].upper() if len(cinema_id) >= 3 else ""
        return prefix_map.get(prefix, prefix)

    # =========================================================================
    # DETAIL CACHE
    # =========================================================================

    async def _get_detail_cache(
        self, client: AsyncHTTPClient, city_id: str
    ) -> dict[str, dict]:
        """
        GET /api/movies?type=now-playing&city_id={id}
        Cache per kota: {movie_code → film_dict}
        """
        async with self._cache_lock:
            if city_id in self._detail_cache:
                return self._detail_cache[city_id]
            try:
                resp = await client.get(
                    f"{MOBILE_BASE}/api/movies?type=now-playing&city_id={city_id}",
                    headers=MOBILE_HEADERS,
                )
                self.logger.info(
                    f"detail_cache city_id={city_id}: "
                    f"status={resp.status_code} len={len(resp.text)}"
                )
                if resp.status_code != 200:
                    self._detail_cache[city_id] = {}
                    return {}
                data = resp.json()
                if data.get("status") != "OK":
                    self._detail_cache[city_id] = {}
                    return {}
                content = (
                    data.get("data", {}).get("value", {}).get("content") or []
                )
                cache = {
                    f.get("dc21_parent_movie_code"): f
                    for f in content
                    if f.get("dc21_parent_movie_code")
                }
                self.logger.info(
                    f"detail_cache city_id={city_id}: {len(cache)} film"
                )
                self._detail_cache[city_id] = cache
                return cache
            except Exception as e:
                self.logger.warning(
                    f"detail_cache city_id={city_id}: {type(e).__name__}: {e}"
                )
                self._detail_cache[city_id] = {}
                return {}

    # =========================================================================
    # SCRAPE MOVIES
    # =========================================================================

    async def scrape_movies(
        self, client: AsyncHTTPClient, cinema: Cinema
    ) -> tuple[list[CinemaMovie], list[Showtime]]:
        if not cinema.external_id:
            self.logger.warning(f"Skip {cinema.name}: external_id kosong")
            return [], []

        # Utamakan schedule dari cache Playwright (intercept saat navigate /cinemas/{id})
        # karena getTheaterSchedule via httpx POST selalu return kosong (butuh session).
        schedule_days = getattr(self, "_schedule_cache", {}).get(cinema.external_id)

        # Fallback ke dc21-api jika tidak ada di cache (seharusnya tidak terjadi)
        if not schedule_days:
            self.logger.debug(
                f"[{cinema.name}] tidak ada di schedule_cache, fallback ke dc21-api"
            )
            schedule_days = await self._fetch_schedule_days(client, cinema)
        if not schedule_days:
            return [], []

        city_id      = self._resolve_city_id(cinema, self._city_map_cache)
        detail_cache = (
            await self._get_detail_cache(client, city_id) if city_id else {}
        )

        return self._parse_schedule(schedule_days, cinema, detail_cache)

    async def _fetch_schedule_days(
        self, client: AsyncHTTPClient, cinema: Cinema
    ) -> list[dict]:
        """GET dc21-api /cinema/schedule/theater?cinema_id={id}"""
        try:
            resp = await client.get(
                f"{DC21_API}/cinema/schedule/theater",
                headers=MOBILE_HEADERS,
                params={"cinema_id": cinema.external_id},
            )
            self.logger.info(
                f"dc21 schedule [{cinema.name}]: "
                f"status={resp.status_code} len={len(resp.text)}"
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not data.get("data", {}).get("is_success"):
                return []
            return data.get("data", {}).get("value") or []
        except Exception as e:
            self.logger.warning(
                f"dc21 schedule [{cinema.name}]: {type(e).__name__}: {e}"
            )
            return []

    async def _fetch_schedule_days_mobile(
        self, client: AsyncHTTPClient, cinema: Cinema
    ) -> list[dict]:
        """Fallback: POST /api/theater?type=getTheaterSchedule"""
        try:
            resp = await client.post(
                f"{MOBILE_BASE}/api/theater?type=getTheaterSchedule",
                headers=MOBILE_HEADERS,
                json={"cinema_id": cinema.external_id},
            )
            self.logger.info(
                f"mobile schedule [{cinema.name}]: "
                f"status={resp.status_code} len={len(resp.text)}"
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not data.get("data", {}).get("is_success"):
                return []
            return data.get("data", {}).get("value") or []
        except Exception as e:
            self.logger.warning(
                f"mobile schedule [{cinema.name}]: {type(e).__name__}: {e}"
            )
            return []

    def _parse_schedule(
        self,
        schedule_days: list,
        cinema: Cinema,
        detail_cache: dict[str, dict],
    ) -> tuple[list[CinemaMovie], list[Showtime]]:
        cinema_movies: list[CinemaMovie] = []
        showtimes: list[Showtime]        = []
        seen_cm: set[tuple] = set()

        for day in schedule_days:
            show_date   = self._parse_date(day.get("date") or today_iso())
            cinema_subs = day.get("cinema") or {}

            for sub_key, film_list in cinema_subs.items():
                if not isinstance(film_list, list):
                    continue
                fmt_sub = self._subtype_to_format(sub_key)

                for film in film_list:
                    cafe_group = film.get("cafe_group", "")
                    # cafe_group bisa berisi parent group ID (mis. untuk bioskop
                    # multi-gedung). Kita skip hanya jika cafe_group terisi DAN
                    # tidak mengandung external_id cinema ini (substring, bukan
                    # exact match) — mencegah film ter-skip akibat perbedaan
                    # format ID antara parent group dan cinema individual.
                    if cafe_group and cinema.external_id not in cafe_group and cafe_group not in cinema.external_id:
                        continue

                    title = clean_text(
                        film.get("title") or
                        film.get("dc21_parent_movie_code") or ""
                    )
                    if not title:
                        continue

                    movie_code = clean_text(film.get("dc21_parent_movie_code") or "")
                    fmt = (
                        fmt_sub if fmt_sub != "2D"
                        else normalize_format(film.get("movie_type") or "2D")
                    )
                    dur_raw    = str(film.get("duration") or "").strip()
                    duration   = f"{dur_raw} menit" if dur_raw.isdigit() else extract_duration(dur_raw)
                    age_rating = normalize_age_rating(
                        film.get("rating") or film.get("age_limit") or ""
                    )

                    cm_key = (title, fmt, show_date)
                    if cm_key in seen_cm:
                        existing_cm = next(
                            (cm for cm in cinema_movies
                             if (cm.title, cm.format, cm.show_date) == cm_key),
                            None,
                        )
                        if existing_cm:
                            self._append_showtimes(showtimes, existing_cm, film, show_date, fmt)
                        continue
                    seen_cm.add(cm_key)

                    detail = detail_cache.get(movie_code, {})
                    cm = CinemaMovie(
                        cinema_id  = cinema.id,
                        title      = title,
                        source     = self.SOURCE,
                        movie_code = movie_code,
                        genre      = clean_text(film.get("genre") or detail.get("genre") or ""),
                        duration   = duration,
                        age_rating = age_rating,
                        format     = fmt,
                        show_date  = show_date,
                    )
                    cm._raw_detail = {
                        "movie_code":  movie_code,
                        "title":       title,
                        "overview":    clean_text(detail.get("synopsis") or ""),
                        "runtime":     int(dur_raw) if dur_raw.isdigit() else None,
                        "poster_path": detail.get("movie_image") or "",
                        "trailer_key": detail.get("trailer") or "",
                        "cast_raw":    clean_text(detail.get("player") or ""),
                        "director":    clean_text(detail.get("director") or ""),
                        "producer":    clean_text(detail.get("producer") or ""),
                        "writer":      clean_text(detail.get("writer") or ""),
                        "sub_type":    detail.get("sub_type") or {},
                    }
                    cinema_movies.append(cm)
                    self._append_showtimes(showtimes, cm, film, show_date, fmt)

        return cinema_movies, showtimes

    def _append_showtimes(
        self, showtimes: list, cm: CinemaMovie,
        film: dict, show_date: str, fmt: str,
    ) -> None:
        """
        Struktur schedule dari dc21-api (terkonfirmasi dari debug):
        film["schedule"] = [
          {
            "movie_type_name": "Reguler 2D",
            "ticket_price":    "60000",
            "time_show":       ["12:05", "14:25", "18:45", "21:10"],
            "studio_id":       [5, 5, 5, 6],
            "show_status":     ["0", "0", "0", "0"],
            "seat_available":  [{"free_seat": -1, "total_seat": -1}, ...]
          }
        ]

        Tiap item di schedule = satu subtype/format (bisa Reguler 2D, IMAX, dll).
        time_show = list jam tayang untuk subtype tersebut.
        """
        schedule_list = film.get("schedule") or []

        for sched in schedule_list:
            # Format dari movie_type_name lebih spesifik dari sub_key
            # e.g. "Reguler 2D", "IMAX", "The Premiere"
            type_name = clean_text(sched.get("movie_type_name") or "")
            sched_fmt = normalize_format(type_name) if type_name else fmt

            time_shows   = sched.get("time_show") or []
            studio_ids   = sched.get("studio_id") or []
            ticket_price = sched.get("ticket_price") or ""

            for i, t in enumerate(time_shows):
                time_str = str(t).strip()
                if not re.match(r'^\d{1,2}:\d{2}$', time_str):
                    self.logger.debug(f"Skip invalid time: {time_str!r}")
                    continue
                h, m     = time_str.split(":")
                time_str = f"{int(h):02d}:{m}"

                studio_id = studio_ids[i] if i < len(studio_ids) else None

                # Parse ticket_price ke integer
                try:
                    price = int(ticket_price) if ticket_price else None
                except (ValueError, TypeError):
                    price = None

                showtimes.append(Showtime(
                    cinema_movie_id = cm.id,
                    cinema_id       = cm.cinema_id,
                    show_date       = show_date,
                    show_time       = time_str,
                    format          = sched_fmt,
                    source          = self.SOURCE,
                    studio_id       = studio_id,
                    ticket_price    = price,
                ))

    # =========================================================================
    # UPCOMING
    # =========================================================================

    async def scrape_upcoming(self, client: AsyncHTTPClient) -> list[dict]:
        """GET /api/movies?type=upcoming"""
        try:
            resp = await client.get(
                f"{MOBILE_BASE}/api/movies?type=upcoming",
                headers=MOBILE_HEADERS,
            )
            self.logger.info(f"upcoming: status={resp.status_code} len={len(resp.text)}")
            if resp.status_code != 200:
                return []
            data    = resp.json()
            content = data.get("data", {}).get("value", {}).get("content") or []
            self.logger.info(f"upcoming: {len(content)} film")
            return content
        except Exception as e:
            self.logger.warning(f"upcoming: {type(e).__name__}: {e}")
            return []

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _resolve_city_id(self, cinema: Cinema, city_map: dict[str, int]) -> str:
        """
        Ambil city_id valid untuk sebuah cinema.

        Urutan lookup:
          1. Dari booking_url (?city_id=N) — sudah tersimpan saat scrape_cinemas
          2. Dari city_map dengan nama kota cinema (case-insensitive substring)

        Return string kosong jika tidak ditemukan, agar caller bisa skip
        detail_cache lookup daripada call dengan city_id=0 yang tidak valid.
        """
        # 1. Dari booking_url (?city_id=N)
        m = re.search(r'city_id=(\d+)', cinema.booking_url or "")
        cid = m.group(1) if m else ""
        if cid and cid != "0":
            return cid

        # 2. Fallback: cari di city_map berdasarkan nama kota cinema
        if cinema.city:
            city_lower = cinema.city.lower()
            for map_name, map_id in city_map.items():
                if map_id and city_lower in map_name.lower():
                    return str(map_id)
                if map_name.lower() in city_lower:
                    return str(map_id)

        return ""

    @staticmethod
    def _parse_date(raw: str) -> str:
        try:
            parts = raw.split("-")
            if len(parts) == 3 and len(parts[2]) == 4:
                return f"{parts[2]}-{parts[1]}-{parts[0]}"
            return raw
        except Exception:
            return today_iso()

    @staticmethod
    def _subtype_to_format(sub_key: str) -> str:
        key = sub_key.lower()
        if "imax" in key:
            return "IMAX"
        if "premiere" in key:
            return "Premiere"
        return "2D"