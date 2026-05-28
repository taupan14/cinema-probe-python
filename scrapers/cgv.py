"""
scrapers/cgv.py — v5 (production)

Integrasi penuh dengan BaseScraper project:
  - Override run() agar pakai CGVSession (cookie + CSRF Laravel) bukan AsyncHTTPClient
  - scrape_cinemas() dan scrape_movies() tetap ada sebagai abstract method fulfillment
  - Parse HTML /en/schedule/cinema/{id}:
      .schedule-title  → judul, genre, durasi, movie_code (CGV ID)
      .schedule-type   → format (2D/4DX2D/IMAX/dll) + hall
      .showtime-lists  → jam tayang per format
  - Fetch age_rating dari /en/movies/info/{movie_code} dengan cache
  - show_times disimpan sebagai JSON array string: '["13:00","17:40"]'
  - Fallback: POST /en/loader/home_movie_list jika HTML kosong
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import logging
from datetime import date
from typing import NamedTuple

import httpx
from bs4 import BeautifulSoup

from models import Cinema, CinemaMovie, Showtime, ScrapeResult
from utils import (
    AsyncHTTPClient, clean_text, normalize_city,
    extract_duration, normalize_format, normalize_age_rating, today_iso,
)
from scrapers.base import BaseScraper

CGV_BASE         = "https://www.cgv.id"
CGV_SCHEDULE     = f"{CGV_BASE}/en/schedule/cinema"
CGV_MOVIE_LOADER = f"{CGV_BASE}/en/loader/home_movie_list"
CGV_MOVIE_INFO   = f"{CGV_BASE}/en/movies/info"

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}

_BLACKLIST: set[str] = {
    "now showing", "coming soon", "trailer", "beli tiket", "buy ticket",
    "jadwal", "schedule", "bioskop", "cinema", "home", "menu", "login",
    "register", "search", "cgv", "info", "about", "contact", "promo",
    "news", "faq", "syarat", "privacy", "copyright", "pick your seats",
}


class ShowtimeEntry(NamedTuple):
    show_time:    str
    format:       str
    hall:         str
    showtime_id:  str
    audi_type_id: str   # attr-audi-type-id dari HTML, misal "01", "03"
    ticket_price: int | None = None  # diisi setelah parse price table


# ─────────────────────────────────────────────────────────────────────────────
# CGV Session — cookie + CSRF Laravel
# ─────────────────────────────────────────────────────────────────────────────

class CGVSession:
    """
    Manages httpx.AsyncClient dengan:
    - Warm-up GET /en/ → WAF cookies (HWWAFSESID) + Laravel XSRF-TOKEN
    - Setiap POST: header X-XSRF-TOKEN = url_unquote(cookie XSRF-TOKEN)
    - Auto-refresh token jika response 419
    """

    def __init__(self, delay: float = 1.0, timeout: float = 30.0):
        self.delay   = delay
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._xsrf   = ""

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=_BASE_HEADERS,
        )
        await self._warmup()
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def _warmup(self):
        r = await self._client.get(
            f"{CGV_BASE}/en/",
            headers={**_BASE_HEADERS, "Accept": "text/html,*/*;q=0.9"},
        )
        r.raise_for_status()
        self._refresh_xsrf()

    def _refresh_xsrf(self):
        raw = self._client.cookies.get("XSRF-TOKEN", "")
        self._xsrf = urllib.parse.unquote(raw)

    def _html_h(self, referer: str = CGV_BASE) -> dict:
        return {
            **_BASE_HEADERS,
            "Accept":   "text/html,application/xhtml+xml,*/*;q=0.9",
            "Referer":  referer,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        }

    def _ajax_h(self, referer: str = CGV_SCHEDULE) -> dict:
        return {
            **_BASE_HEADERS,
            "Accept":           "application/json, text/javascript, */*; q=0.01",
            "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN":     self._xsrf,
            "Origin":           CGV_BASE,
            "Referer":          referer,
            "Sec-Fetch-Dest":   "empty",
            "Sec-Fetch-Mode":   "cors",
            "Sec-Fetch-Site":   "same-origin",
        }

    async def get(self, url: str, referer: str = CGV_BASE) -> httpx.Response:
        await asyncio.sleep(self.delay)
        return await self._client.get(url, headers=self._html_h(referer))

    async def post_ajax(self, url: str, data: dict, referer: str = CGV_SCHEDULE) -> httpx.Response:
        await asyncio.sleep(self.delay)
        r = await self._client.post(url, data=data, headers=self._ajax_h(referer))
        if r.status_code == 419:
            await self._warmup()
            r = await self._client.post(url, data=data, headers=self._ajax_h(referer))
        return r


# ─────────────────────────────────────────────────────────────────────────────
# CGV Scraper
# ─────────────────────────────────────────────────────────────────────────────

class CGVScraper(BaseScraper):
    SOURCE   = "cgv"
    CHAIN    = "CGV"
    BASE_URL = CGV_BASE

    def __init__(self, **kwargs):
        super().__init__(concurrency=5, **kwargs)
        # Cache age_rating per movie_code agar tidak fetch ulang di tiap bioskop
        self._age_cache: dict[str, str] = {}

    # ── Override run() — pakai CGVSession bukan AsyncHTTPClient ──────────────

    async def run(self) -> ScrapeResult:
        """
        Override BaseScraper.run() karena CGV butuh CGVSession
        (cookie + CSRF Laravel) bukan AsyncHTTPClient biasa.
        """
        result = ScrapeResult(source=self.SOURCE)
        self.logger.info(f"🎬 Starting scrape: {self.SOURCE} ({self.CHAIN})")

        async with CGVSession(delay=self.delay, timeout=self.timeout) as session:

            # ── Cinemas ───────────────────────────────────────────────────
            try:
                cinemas = await self._fetch_cinemas(session)
                result.cinemas = cinemas
                self.logger.info(f"✅ Found {len(cinemas)} cinemas")
            except Exception as e:
                msg = f"scrape_cinemas failed: {e}"
                self.logger.exception(msg)
                result.errors.append(msg)
                return result

            if not cinemas:
                self.logger.warning("CGV: 0 cinemas ditemukan")
                return result

            # ── Movies (concurrent, dibatasi semaphore) ───────────────────
            tasks = [
                self._scrape_movies_safe_cgv(session, c, i, len(cinemas))
                for i, c in enumerate(cinemas, 1)
            ]
            for movies in await asyncio.gather(*tasks):
                for m in movies:
                    result.movies.append(m)
                    # Konversi showtimes_detail → Showtime objects → result.showtimes
                    for entry in getattr(m, "showtimes_detail", []):
                        # studio_id: attr-audi-type-id dari HTML ("01", "03", dll) → int
                        studio_id = None
                        if entry.audi_type_id:
                            try:
                                studio_id = int(entry.audi_type_id)
                            except ValueError:
                                pass

                        result.showtimes.append(Showtime(
                            cinema_movie_id = m.id,
                            cinema_id       = m.cinema_id,
                            show_date       = m.show_date,
                            show_time       = entry.show_time,
                            format          = entry.format,
                            source          = m.source,
                            studio_id       = studio_id,
                            ticket_price    = entry.ticket_price,
                        ))

        self.logger.info(
            f"✅ Done {self.SOURCE}: "
            f"{result.cinema_count} cinemas, {result.movie_count} movies, "
            f"{result.showtime_count} showtimes"
        )
        return result

    # ── Fulfillment abstract methods (dipanggil BaseScraper jika tidak di-override) ──

    async def scrape_cinemas(self, client: AsyncHTTPClient) -> list[Cinema]:
        """Fallback jika dipanggil langsung oleh BaseScraper.run()."""
        async with CGVSession(self.delay, self.timeout) as s:
            return await self._fetch_cinemas(s)

    async def scrape_movies(self, client: AsyncHTTPClient, cinema: Cinema) -> list[CinemaMovie]:
        """Fallback jika dipanggil langsung oleh BaseScraper.run()."""
        async with CGVSession(self.delay, self.timeout) as s:
            return await self._fetch_movies(s, cinema)

    # ── Cinema list ───────────────────────────────────────────────────────────

    async def _fetch_cinemas(self, session: CGVSession) -> list[Cinema]:
        resp = await session.get(CGV_SCHEDULE, referer=f"{CGV_BASE}/en/")
        resp.raise_for_status()
        cinemas = self._parse_cinema_list(BeautifulSoup(resp.text, "lxml"))
        self.logger.info(f"CGV: {len(cinemas)} cinemas parsed")
        return cinemas

    def _parse_cinema_list(self, soup: BeautifulSoup) -> list[Cinema]:
        cinemas: list[Cinema] = []
        seen:    set[str]     = set()

        for link in soup.select("a[href*='/en/schedule/cinema/']"):
            href = link.get("href", "")
            m    = re.search(r"/en/schedule/cinema/(\w+)/?$", href)
            if not m:
                continue
            ext_id = m.group(1)
            if ext_id in seen:
                continue
            seen.add(ext_id)

            name = clean_text(link.get_text())
            if not name:
                continue

            full_url  = href if href.startswith("http") else f"{CGV_BASE}{href}"
            full_name = f"CGV {name}" if not name.upper().startswith("CGV") else name

            cinemas.append(Cinema(
                name=full_name,
                chain=self.CHAIN,
                city=self._extract_city(link),
                source=self.SOURCE,
                external_id=ext_id,
                booking_url=full_url,
            ))

        return cinemas

    def _extract_city(self, link) -> str:
        try:
            li      = link.find_parent("li")
            ul      = li.find_parent("ul") if li else None
            city_li = ul.find_parent("li") if ul else None
            if city_li:
                a = city_li.find("a", href=re.compile(r"javascript|#"))
                if a:
                    return normalize_city(clean_text(a.get_text()))
                for tag in ["strong", "span", "h2", "h3", "h4"]:
                    el = city_li.find(tag)
                    if el:
                        return normalize_city(clean_text(el.get_text()))
        except Exception:
            pass
        return ""

    # ── Movie list ────────────────────────────────────────────────────────────

    async def _scrape_movies_safe_cgv(
        self, session: CGVSession, cinema: Cinema, index: int, total: int
    ) -> list[CinemaMovie]:
        async with self.semaphore:
            try:
                movies = await self._fetch_movies(session, cinema)
                self.logger.info(f"[{index}/{total}] {cinema.name} → {len(movies)} movies")
                return movies
            except Exception as e:
                self.logger.warning(
                    f"[{index}/{total}] {cinema.name} failed: {type(e).__name__}: {e}"
                )
                return []

    async def _fetch_movies(self, session: CGVSession, cinema: Cinema) -> list[CinemaMovie]:
        """
        Strategi A (utama): GET /en/schedule/cinema/{id} → parse HTML
        Strategi B (fallback): POST /en/loader/home_movie_list
        """
        if not cinema.external_id:
            return []

        url  = f"{CGV_SCHEDULE}/{cinema.external_id}"
        resp = await session.get(url, referer=CGV_SCHEDULE)

        if resp.status_code != 200:
            return await self._fallback_loader(session, cinema)

        movies = self._parse_schedule_html(resp.text, cinema)

        # Enrich age_rating dari halaman detail film
        if movies:
            await self._enrich_age_rating(session, movies)

        if not movies:
            movies = await self._fallback_loader(session, cinema)

        return movies

    # ── Parser HTML schedule ──────────────────────────────────────────────────

    def _parse_schedule_html(self, html: str, cinema: Cinema) -> list[CinemaMovie]:
        """
        Struktur HTML (dari probe):
        <div class="schedule-lists">
          <ul>
            <li>
              <div class="schedule-title">
                <a href="/en/movies/info/26018200/...">MORTAL KOMBAT II</a>
                <span>ACTION / 116 Minutes</span>
              </div>
              <ul>
                <li class="schedule-type">
                  <i class="fa fa-caret-right"></i> 4DX2D
                  <span class="audi-nm">4DX 5</span>
                </li>
                <li>
                  <ul class="showtime-lists">
                    <li>
                      <a id="9706966" attr-fmt="4DX2D" attr-audi-type-name="4DX 5">13:00</a>
                    </li>
                  </ul>
                </li>
              </ul>
            </li>
          </ul>
        </div>
        """
        soup      = BeautifulSoup(html, "lxml")
        container = soup.select_one(".schedule-lists")
        if not container:
            return []

        # Parse price table satu kali per halaman (berlaku untuk semua film di cinema ini)
        price_map = self._parse_price_table(soup)
        self.logger.debug(f"Price map: {list(price_map.keys())}")

        movies: list[CinemaMovie] = []
        # Key: (title_upper, format) — satu entry per kombinasi, tanpa duplikat
        seen: set[tuple[str, str]] = set()

        # Setiap <li> yang punya .schedule-title = satu film
        film_items = container.select("ul > li:has(.schedule-title)")
        if not film_items:
            film_items = [
                el.find_parent("li")
                for el in container.select(".schedule-title")
                if el.find_parent("li")
            ]

        for li in film_items:
            title_div = li.select_one(".schedule-title")
            if not title_div:
                continue

            title, genre, duration, movie_code = self._parse_title_block(title_div)
            if not self._valid_title(title):
                continue

            format_groups = self._extract_format_groups(li)

            if not format_groups:
                key = (title.upper(), "2D")
                if key not in seen:
                    seen.add(key)
                    movies.append(self._make_movie(
                        cinema, title, genre, duration, "", "2D", "[]", movie_code, []
                    ))
                continue

            for fmt, hall, showtime_entries in format_groups:
                # Resolve ticket_price per entry berdasarkan hall + hari
                enriched_entries = []
                for entry in showtime_entries:
                    price = self._resolve_ticket_price(price_map, entry.hall, entry.format, today_iso())
                    enriched_entries.append(entry._replace(ticket_price=price))

                key = (title.upper(), fmt)
                if key in seen:
                    # Merge showtimes ke entry yang sudah ada
                    for m in movies:
                        if m.cinema_id == cinema.id and m.title.upper() == title.upper() and m.format == fmt:
                            existing  = json.loads(m.show_times or "[]")
                            new_times = [s.show_time for s in enriched_entries]
                            m.show_times = json.dumps(sorted(set(existing + new_times)))
                            m.showtimes_detail.extend(enriched_entries)
                    continue
                seen.add(key)

                time_list = sorted({s.show_time for s in enriched_entries})
                movies.append(self._make_movie(
                    cinema, title, genre, duration, "", fmt,
                    json.dumps(time_list), movie_code, list(enriched_entries)
                ))

        return movies

    def _parse_title_block(self, title_div) -> tuple[str, str, str, str]:
        """
        <div class="schedule-title">
          <a href="/en/movies/info/26018200/...">MORTAL KOMBAT II</a>
          <span>ACTION / 116 Minutes</span>
        </div>
        → (title, genre, duration, movie_code)
        """
        a_el = title_div.select_one("a[href*='/movies/info/']")
        if a_el:
            title      = clean_text(a_el.get_text())
            m          = re.search(r"/movies/info/(\d+)", a_el.get("href", ""))
            movie_code = m.group(1) if m else ""
        else:
            title      = clean_text(title_div.get_text())
            movie_code = ""

        span   = title_div.select_one("span")
        genre, duration = "", ""
        if span:
            raw   = clean_text(span.get_text())
            parts = [p.strip() for p in raw.split("/")]
            if len(parts) >= 2:
                genre    = parts[0].title()
                duration = extract_duration(parts[1])
            elif parts:
                if re.search(r"\d+\s*min", parts[0], re.I):
                    duration = extract_duration(parts[0])
                else:
                    genre = parts[0].title()

        return title, genre, duration, movie_code

    def _extract_format_groups(self, li) -> list[tuple[str, str, list[ShowtimeEntry]]]:
        """
        Dari setiap film <li>, kumpulkan semua format + jam.
        Setiap <ul> di dalam film <li> = satu format.
        """
        groups: list[tuple[str, str, list[ShowtimeEntry]]] = []

        for ul in li.find_all("ul", recursive=False):
            type_li = ul.select_one("li.schedule-type") or ul.find("li")
            if not type_li:
                continue

            audi_nm  = type_li.select_one(".audi-nm")
            hall     = clean_text(audi_nm.get_text()) if audi_nm else ""
            type_txt = clean_text(type_li.get_text())
            if hall:
                type_txt = type_txt.replace(hall, "").strip()
            type_txt = re.sub(r"^[^\w]+", "", type_txt).strip()
            fmt      = normalize_format(type_txt) if type_txt else "2D"

            entries: list[ShowtimeEntry] = []
            for a in ul.select(".showtime-lists li a"):
                t = clean_text(a.get_text())
                if not re.match(r"^\d{1,2}:\d{2}$", t):
                    continue
                entries.append(ShowtimeEntry(
                    show_time    = t,
                    format       = normalize_format(a.get("attr-fmt", fmt)),
                    hall         = a.get("attr-audi-type-name", hall),
                    showtime_id  = a.get("id", ""),
                    audi_type_id = a.get("attr-audi-type-id", ""),
                ))

            if entries:
                actual_fmt = entries[0].format
                groups.append((actual_fmt, hall, entries))

        return groups

    def _parse_price_table(self, soup: BeautifulSoup) -> dict[str, dict[str, int]]:
        """
        Parse .price_group → .table_price untuk mendapatkan harga per auditorium-type per hari.

        Struktur HTML (dari probe):
          .price_group
            .title-price-info  → "REGULAR", "STARIUM", "4DX", "GOLD CLASS", "VELVET", ...
            .table_price
              <tr><td>Mon-Thu</td><td>:</td><td>Rp. 56.000</td></tr>
              <tr><td>Friday</td><td>:</td><td>Rp. 66.000</td></tr>
              <tr><td>Weekend/Holiday</td><td>:</td><td>Rp. 76.000</td></tr>

        Returns:
          {
            "regular":    {"weekday": 56000, "friday": 66000, "weekend": 76000},
            "starium":    {"weekday": 61000, "friday": 71000, "weekend": 81000},
            "4dx":        {"weekday": 95000, "friday": 125000, "weekend": 135000},
            "gold class": {...},
            ...
          }
        """
        price_map: dict[str, dict[str, int]] = {}

        columns = soup.select(".col-prices-cinema-sch .column, .sub_group_price .column")
        if not columns:
            # Fallback: cari semua pasangan title-price-info + table_price
            columns = soup.select(".price_group .column")

        for col in columns:
            title_el = col.select_one(".title-price-info")
            if not title_el:
                continue
            audi_type = clean_text(title_el.get_text()).lower()  # "regular", "starium", "4dx"

            prices: dict[str, int] = {}
            for row in col.select(".table_price tr"):
                cells = row.select("td")
                if len(cells) < 3:
                    continue
                day_label = clean_text(cells[0].get_text()).lower()  # "mon-thu", "friday", "weekend/holiday"
                price_str = clean_text(cells[-1].get_text())         # "Rp. 56.000"

                # Konversi "Rp. 56.000" → 56000
                price_num = re.sub(r"[^0-9]", "", price_str)
                if not price_num:
                    continue
                price = int(price_num)

                if "fri" in day_label:
                    prices["friday"]  = price
                elif "weekend" in day_label or "holiday" in day_label or "sat" in day_label:
                    prices["weekend"] = price
                else:
                    prices["weekday"] = price  # mon-thu

            if prices:
                price_map[audi_type] = prices

        return price_map

    def _resolve_ticket_price(
        self, price_map: dict[str, dict[str, int]], hall: str, fmt: str, show_date: str
    ) -> int | None:
        """
        Pilih harga yang tepat berdasarkan hall/format dan hari tayang.

        Strategi lookup (dari spesifik ke umum):
          1. Cocokkan hall name ke audi_type ("Gold Class 9" → "gold class")
          2. Cocokkan format ke audi_type ("4DX2D" → "4dx", "IMAX" → "imax")
          3. Fallback ke "regular"

        Hari:
          - Sabtu/Minggu/Hari Libur → weekend
          - Jumat                    → friday
          - Senin-Kamis              → weekday
        """
        if not price_map:
            return None

        # Tentukan kategori hari dari show_date
        try:
            from datetime import date as _date
            d = _date.fromisoformat(show_date)
            weekday = d.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
            if weekday >= 5:       # Sabtu/Minggu
                day_key = "weekend"
            elif weekday == 4:     # Jumat
                day_key = "friday"
            else:
                day_key = "weekday"
        except Exception:
            day_key = "weekday"

        # Mapping hall name / format → audi_type key di price_map
        hall_lower = hall.lower()
        fmt_lower  = fmt.lower().replace(" ", "")

        # Cari kecocokan berdasarkan hall name
        # Prioritas: nama persis → substring
        AUDI_KEYWORDS = [
            ("gold class",  ["gold class", "gold"]),
            ("starium",     ["starium"]),
            ("4dx",         ["4dx"]),
            ("screenx",     ["screenx"]),
            ("velvet",      ["velvet"]),
            ("satin",       ["satin"]),
            ("imax",        ["imax"]),
            ("3d",          ["3d"]),
        ]

        audi_key = None

        # 1. Cek dari hall name
        for key, keywords in AUDI_KEYWORDS:
            if any(kw in hall_lower for kw in keywords):
                audi_key = key
                break

        # 2. Cek dari format jika hall tidak match
        if not audi_key:
            for key, keywords in AUDI_KEYWORDS:
                if any(kw in fmt_lower for kw in keywords):
                    audi_key = key
                    break

        # 3. Fallback ke regular
        if not audi_key or audi_key not in price_map:
            audi_key = "regular"

        prices = price_map.get(audi_key, price_map.get("regular", {}))
        return prices.get(day_key) or prices.get("weekday")

    def _make_movie(
        self, cinema: Cinema, title: str, genre: str, duration: str,
        age_rating: str, fmt: str, show_times: str,
        movie_code: str, showtimes_detail: list
    ) -> CinemaMovie:
        m = CinemaMovie(
            cinema_id  = cinema.id,
            title      = title,
            genre      = genre,
            duration   = duration,
            age_rating = age_rating,
            format     = fmt,
            source     = self.SOURCE,
            show_date  = today_iso(),
        )
        # Field tambahan — diset via setattr agar aman jika model tidak punya
        m.show_times       = show_times
        m.movie_code       = movie_code
        m.showtimes_detail = showtimes_detail
        return m

    # ── Enrich age_rating ─────────────────────────────────────────────────────

    async def _enrich_age_rating(
        self, session: CGVSession, movies: list[CinemaMovie]
    ) -> None:
        """
        Fetch /en/movies/info/{movie_code} untuk age_rating.
        Cache per movie_code agar tidak di-fetch berulang.
        """
        to_fetch = [
            m for m in movies
            if getattr(m, "movie_code", "") and
               getattr(m, "movie_code", "") not in self._age_cache
        ]

        sem = asyncio.Semaphore(5)

        async def fetch_one(movie: CinemaMovie) -> None:
            async with sem:
                code = movie.movie_code
                try:
                    r = await session.get(
                        f"{CGV_MOVIE_INFO}/{code}",
                        referer=CGV_SCHEDULE,
                    )
                    self._age_cache[code] = (
                        self._extract_age_rating(r.text)
                        if r.status_code == 200 else ""
                    )
                except Exception:
                    self._age_cache[code] = ""

        await asyncio.gather(*[fetch_one(m) for m in to_fetch])

        for m in movies:
            code = getattr(m, "movie_code", "")
            if code in self._age_cache:
                m.age_rating = self._age_cache[code]

    def _extract_age_rating(self, html: str) -> str:
        """
        Dari probe: .movie-info-wrapper teks mengandung "CENSOR RATING: D17"
        """
        soup    = BeautifulSoup(html, "lxml")
        wrapper = soup.select_one(
            ".movie-info-wrapper, .movie-add-info, [class*='movie-info']"
        )
        if not wrapper:
            return ""
        text = clean_text(wrapper.get_text(" "))

        m = re.search(r"censor\s*rating\s*[:：]\s*([A-Z0-9\-+]+)", text, re.I)
        if m:
            return normalize_age_rating(m.group(1))

        for rating in ["D17", "17+", "21+", "SU", "BO", "RD", "R"]:
            if re.search(rf"\b{re.escape(rating)}\b", text, re.I):
                return rating
        return ""

    # ── Fallback loader ───────────────────────────────────────────────────────

    async def _fallback_loader(
        self, session: CGVSession, cinema: Cinema
    ) -> list[CinemaMovie]:
        """
        POST /en/loader/home_movie_list → {"now_playing": "<ul>...</ul>"}
        Judul dari slug ?title= di URL anchor.
        """
        try:
            resp = await session.post_ajax(
                CGV_MOVIE_LOADER,
                {"cinema_id": cinema.external_id},
                referer=f"{CGV_SCHEDULE}/{cinema.external_id}",
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception as e:
            self.logger.debug(f"CGV loader {cinema.name}: {e}")
            return []

        html_frag = data.get("now_playing", "")
        if not html_frag:
            return []

        soup    = BeautifulSoup(html_frag, "lxml")
        movies: list[CinemaMovie] = []
        seen:   set[str]          = set()

        for a in soup.select("a[href*='/movies/info/']"):
            href = a.get("href", "")
            m_code = re.search(r"/movies/info/(\d+)", href)
            movie_code = m_code.group(1) if m_code else ""

            m_title = re.search(r"[?&]title=([^&]+)", href)
            title   = (
                urllib.parse.unquote(m_title.group(1)).replace("-", " ").strip().upper()
                if m_title else clean_text(a.get_text())
            )

            if not self._valid_title(title) or title in seen:
                continue
            seen.add(title)

            movies.append(self._make_movie(
                cinema, title, "", "", "", "2D", "[]", movie_code, []
            ))

        return movies

    # ── Validation ────────────────────────────────────────────────────────────

    def _valid_title(self, title: str) -> bool:
        if not title or len(title) < 2 or len(title) > 150:
            return False
        if title.lower().strip() in _BLACKLIST:
            return False
        if title.isdigit() or "http" in title or "@" in title:
            return False
        return True