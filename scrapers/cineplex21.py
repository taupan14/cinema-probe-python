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

    # =========================================================================
    # SCRAPE CINEMAS — Playwright (wajib, karena getAllTheater butuh session)
    # =========================================================================

    async def scrape_cinemas(self, client: AsyncHTTPClient) -> list[Cinema]:
        """
        Gunakan Playwright untuk fetch daftar theater karena:
          POST /api/theater?type=getAllTheater → value=null tanpa session browser

        Strategi:
          1. Buka /cinemas via Playwright
          2. Intercept response getAllTheater yang otomatis dipanggil app → parse JSON
          3. Fallback: parse link /cinemas/{cinema_id} dari HTML yang di-render
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.logger.error(
                "playwright tidak terinstall. Jalankan: "
                "pip install playwright && playwright install chromium"
            )
            return []

        cinemas: list[Cinema] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=MOBILE_HEADERS["User-Agent"],
                viewport={"width": 390, "height": 844},
            )
            page = await context.new_page()

            # ── Intercept getAllTheater response ───────────────────────────
            theater_data: list[dict] = []
            city_map: dict[str, int] = {}    # cinema_id → city_id

            async def on_response(resp):
                url = resp.url
                if "getAllTheater" in url:
                    try:
                        body = await resp.text()
                        data = json.loads(body)
                        value = (
                            data.get("data", {}).get("value") or []
                        )
                        if isinstance(value, list) and value:
                            self.logger.info(
                                f"Intercepted getAllTheater: {len(value)} theaters"
                            )
                            theater_data.extend(value)
                    except Exception as e:
                        self.logger.warning(f"Parse getAllTheater response: {e}")

                # Intercept getCityList untuk mapping city_name → city_id
                if "getCityList" in url:
                    try:
                        body = await resp.text()
                        data = json.loads(body)
                        cities = data.get("data", {}).get("value") or []
                        for c in cities:
                            city_map[c.get("city_name", "").upper()] = c.get("city_id", 0)
                    except Exception:
                        pass

            page.on("response", on_response)

            # ── Buka halaman /cinemas ──────────────────────────────────────
            self.logger.info("Playwright: membuka /cinemas ...")
            try:
                await page.goto(
                    f"{MOBILE_BASE}/cinemas",
                    wait_until="networkidle",
                    timeout=30_000,
                )
                await page.wait_for_timeout(3000)
            except Exception as e:
                self.logger.warning(f"Playwright goto /cinemas: {e}")

            # ── Parse theater_data dari intercept ─────────────────────────
            if theater_data:
                cinemas = self._parse_theater_data(theater_data, city_map)
                self.logger.info(
                    f"XXI: {len(cinemas)} cinema dari getAllTheater intercept"
                )

            # ── Fallback: parse link dari HTML ────────────────────────────
            if not cinemas:
                self.logger.warning(
                    "getAllTheater intercept kosong — fallback ke HTML link parsing"
                )
                cinemas = await self._parse_cinemas_from_page(page, city_map)

            # ── Cari semua kota untuk dapat lebih banyak theater ──────────
            # App hanya load theater untuk kota default (berdasarkan lokasi).
            # Kita perlu klik tiap kota agar getAllTheater dipanggil lagi.
            if cinemas and city_map:
                extra = await self._fetch_theaters_per_city(page, city_map, theater_data)
                existing_ids = {c.external_id for c in cinemas}
                added = 0
                for c in extra:
                    if c.external_id not in existing_ids:
                        existing_ids.add(c.external_id)
                        cinemas.append(c)
                        added += 1
                if added:
                    self.logger.info(f"XXI: +{added} cinema dari city iteration")

            await browser.close()

        self.logger.info(f"XXI: total {len(cinemas)} cinema")
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

                # Cari city_id untuk detail cache
                city_id = city_map.get(city.upper(), 0)

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
                        f"?city_id={city_id}"
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
                city    = self._city_from_cinema_id(cinema_id)
                city_id = city_map.get(city.upper(), 0)
                cinemas.append(Cinema(
                    name        = name,
                    chain       = self.CHAIN,
                    city        = normalize_city(city),
                    source      = self.SOURCE,
                    external_id = cinema_id,
                    booking_url = f"{MOBILE_BASE}/cinemas/{cinema_id}?city_id={city_id}",
                ))
        except Exception as e:
            self.logger.warning(f"HTML fallback: {e}")
        return cinemas

    async def _fetch_theaters_per_city(
        self, page, city_map: dict[str, int], existing: list[dict]
    ) -> list[Cinema]:
        """
        Iterasi tiap kota dengan klik/navigate agar app load theater per kota.
        App memanggil getAllTheater setiap kali user ganti kota di dropdown.
        """
        all_theaters: list[dict] = list(existing)
        seen_ids = {
            sub.get("cinema_id")
            for t in existing
            for sub in (t.get("theater_subtype") or {}).values()
            if isinstance(sub, dict)
        }

        async def on_resp(resp):
            if "getAllTheater" in resp.url:
                try:
                    body = await resp.text()
                    data = json.loads(body)
                    value = data.get("data", {}).get("value") or []
                    for t in (value or []):
                        for sub in (t.get("theater_subtype") or {}).values():
                            if isinstance(sub, dict):
                                cid = sub.get("cinema_id")
                                if cid and cid not in seen_ids:
                                    seen_ids.add(cid)
                                    all_theaters.append(t)
                except Exception:
                    pass

        page.on("response", on_resp)

        # Coba navigate ke beberapa kota besar via URL langsung
        # Format: /cinemas?city_id={N}
        major_city_ids = [
            cid for name, cid in city_map.items()
            if name in {
                "JAKARTA", "BANDUNG", "SURABAYA", "MEDAN", "SEMARANG",
                "YOGYAKARTA", "DENPASAR", "MAKASSAR", "PALEMBANG",
                "TANGERANG", "BEKASI", "DEPOK", "BOGOR", "MALANG",
                "JABODETABEK",
            }
        ]
        for city_id in major_city_ids[:10]:
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

        # Hanya return yang baru (bukan yang sudah ada di existing)
        existing_ids = {
            sub.get("cinema_id")
            for t in existing
            for sub in (t.get("theater_subtype") or {}).values()
            if isinstance(sub, dict)
        }
        new_theaters = [
            t for t in all_theaters
            if any(
                isinstance(sub, dict) and sub.get("cinema_id") not in existing_ids
                for sub in (t.get("theater_subtype") or {}).values()
            )
        ]
        return self._parse_theater_data(new_theaters, city_map)

    @staticmethod
    def _city_from_cinema_id(cinema_id: str) -> str:
        """
        Ekstrak nama kota dari prefix cinema_id (3 huruf pertama).
        Contoh: JKTAETB → Jakarta, BDGPVJ → Bandung, SBYCWO → Surabaya
        """
        prefix_map = {
            "JKT": "Jakarta",   "BDG": "Bandung",   "SBY": "Surabaya",
            "MDN": "Medan",     "SMG": "Semarang",   "YGY": "Yogyakarta",
            "DPS": "Denpasar",  "MKS": "Makassar",   "PLM": "Palembang",
            "BTM": "Batam",     "PKU": "Pekanbaru",  "MLG": "Malang",
            "TGR": "Tangerang", "BKS": "Bekasi",     "DPK": "Depok",
            "BGR": "Bogor",     "SMD": "Samarinda",  "BPN": "Balikpapan",
            "BJM": "Banjarmasin","MNO": "Manado",    "AMB": "Ambon",
            "LPG": "Lampung",   "CRB": "Cirebon",    "KDR": "Kediri",
            "MLG": "Malang",    "PNK": "Pontianak",  "JMB": "Jambi",
            "PDG": "Padang",    "MTR": "Mataram",    "KPG": "Kupang",
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

        schedule_days = await self._fetch_schedule_days(client, cinema)
        if not schedule_days:
            schedule_days = await self._fetch_schedule_days_mobile(client, cinema)
        if not schedule_days:
            return [], []

        city_id      = self._extract_city_id(cinema)
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
                    if cafe_group and cafe_group != cinema.external_id:
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

    @staticmethod
    def _extract_city_id(cinema: Cinema) -> str:
        m = re.search(r'city_id=(\d+)', cinema.booking_url or "")
        return m.group(1) if m else ""

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