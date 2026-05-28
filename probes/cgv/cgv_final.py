"""
scrapers/cgv.py
Scraper CGV Indonesia — hasil reverse engineering via probe.

Temuan dari probe:
  - Framework  : Laravel (bukan CodeIgniter)
  - CSRF       : Cookie XSRF-TOKEN → Header X-XSRF-TOKEN (URL-decoded)
  - WAF        : HWWAFSESID + HWWAFSESTIME harus ikut di setiap request
  - Cinema list: GET  https://www.cgv.id/en/schedule/cinema  (HTML, 72 IDs)
  - Movie list : POST https://www.cgv.id/en/loader/home_movie_list
                 payload : cinema_id=<3-digit>
                 header  : X-XSRF-TOKEN = url_unquote(cookie["XSRF-TOKEN"])
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import logging
from datetime import date

import httpx
from bs4 import BeautifulSoup

# ── Models (sesuaikan dengan models.py project Anda) ──────────────────────────
try:
    from models import Cinema, CinemaMovie, ScrapeResult
    from utils import clean_text, normalize_city, extract_duration, normalize_format, normalize_age_rating
except ImportError:
    # Fallback dataclass agar file bisa dijalankan standalone untuk testing
    from dataclasses import dataclass, field
    from typing import Optional
    import uuid

    @dataclass
    class Cinema:
        name: str = ""
        chain: str = "CGV"
        city: str = ""
        address: str = ""
        source: str = "cgv"
        external_id: str = ""
        booking_url: str = ""
        lat: float = 0.0
        lng: float = 0.0
        id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @dataclass
    class CinemaMovie:
        cinema_id: str = ""
        title: str = ""
        genre: str = ""
        duration: str = ""
        age_rating: str = ""
        format: str = "2D"
        source: str = "cgv"
        show_date: str = ""

    @dataclass
    class ScrapeResult:
        source: str = "cgv"
        cinemas: list = field(default_factory=list)
        movies: list = field(default_factory=list)
        errors: list = field(default_factory=list)

        @property
        def cinema_count(self): return len(self.cinemas)
        @property
        def movie_count(self): return len(self.movies)

    def clean_text(s): return " ".join(str(s).split()).strip()
    def normalize_city(s): return clean_text(s).title()
    def extract_duration(s):
        m = re.search(r"(\d+)", str(s))
        return f"{m.group(1)} min" if m else ""
    def normalize_format(s):
        s = s.upper()
        for fmt in ["IMAX", "4DX", "SCREENX", "4K", "3D"]:
            if fmt in s:
                return fmt
        return "2D"
    def normalize_age_rating(s):
        s = str(s).upper()
        for r in ["SU", "G", "PG", "PG-13", "R", "D17", "17+", "21+"]:
            if r in s:
                return r
        return ""


CGV_BASE          = "https://www.cgv.id"
CGV_SCHEDULE_PAGE = f"{CGV_BASE}/en/schedule/cinema"
CGV_EXECUTE_URL   = f"{CGV_BASE}/en/execute"
CGV_MOVIE_LOADER  = f"{CGV_BASE}/en/loader/home_movie_list"

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Session helper: warm-up + CSRF extraction
# ─────────────────────────────────────────────────────────────────────────────

class CGVSession:
    """
    Manages a single httpx.AsyncClient with:
    - Proper browser headers
    - Automatic CSRF token refresh from cookie XSRF-TOKEN
    - WAF cookie forwarding (HWWAFSESID / HWWAFSESTIME)
    """

    BASE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
    }

    def __init__(self, delay: float = 1.0, timeout: float = 30.0):
        self.delay   = delay
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._xsrf: str = ""

    # ── context manager ───────────────────────────────────────────────────────

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=self.BASE_HEADERS,
        )
        await self._warmup()
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    # ── warm-up: GET homepage → set cookies + CSRF ────────────────────────────

    async def _warmup(self):
        """
        GET CGV homepage agar:
        1. WAF menerima session (HWWAFSESID / HWWAFSESTIME)
        2. Laravel menerbitkan XSRF-TOKEN & cgvweb_session
        3. Kita simpan XSRF-TOKEN → header X-XSRF-TOKEN untuk POST
        """
        logger.info("CGV: warm-up GET homepage...")
        r = await self._client.get(
            f"{CGV_BASE}/en/",
            headers={**self.BASE_HEADERS, "Accept": "text/html,*/*;q=0.9"},
        )
        r.raise_for_status()
        self._refresh_xsrf()
        logger.info(f"CGV: warm-up OK | cookies: {list(self._client.cookies.keys())}")

    def _refresh_xsrf(self):
        """
        Ambil nilai terbaru XSRF-TOKEN dari cookie jar.
        Laravel mengharapkan nilai URL-decoded dikirim di header X-XSRF-TOKEN.
        """
        raw = self._client.cookies.get("XSRF-TOKEN", "")
        self._xsrf = urllib.parse.unquote(raw)
        logger.debug(f"CGV: XSRF-TOKEN refreshed ({len(self._xsrf)} chars)")

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _html_headers(self, referer: str = CGV_BASE) -> dict:
        return {
            **self.BASE_HEADERS,
            "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer":                 referer,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest":          "document",
            "Sec-Fetch-Mode":          "navigate",
            "Sec-Fetch-Site":          "same-origin",
        }

    def _ajax_headers(self, referer: str = CGV_SCHEDULE_PAGE) -> dict:
        """
        Header untuk POST AJAX ke /en/execute dan /en/loader/*.
        Kunci: X-XSRF-TOKEN = url-decoded cookie XSRF-TOKEN.
        """
        return {
            **self.BASE_HEADERS,
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

    async def get(self, url: str, **kwargs) -> httpx.Response:
        await asyncio.sleep(self.delay)
        return await self._client.get(url, headers=self._html_headers(url), **kwargs)

    async def post_ajax(self, url: str, data: dict, referer: str = CGV_SCHEDULE_PAGE) -> httpx.Response:
        """
        POST dengan CSRF token Laravel yang benar.
        Jika mendapat 419, refresh XSRF dari cookie dan coba sekali lagi.
        """
        await asyncio.sleep(self.delay)
        r = await self._client.post(url, data=data, headers=self._ajax_headers(referer))

        if r.status_code == 419:
            # Token expired → refresh dan retry
            logger.debug("CGV: 419 CSRF mismatch → refreshing XSRF token...")
            await self._warmup()
            r = await self._client.post(url, data=data, headers=self._ajax_headers(referer))

        return r


# ─────────────────────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────────────────────

class CGVScraper:
    """
    Scraper CGV Indonesia.

    Alur:
      1. scrape_cinemas()
         GET /en/schedule/cinema → parse HTML → 72 cinema IDs + nama + kota
         Opsional: POST /en/execute dengan action yang benar untuk data lebih lengkap

      2. scrape_movies(cinema)
         POST /en/loader/home_movie_list  payload: cinema_id=<id>
         Response: JSON array film yang sedang tayang
    """

    SOURCE = "cgv"
    CHAIN  = "CGV"

    # Titles yang pasti bukan judul film
    _BLACKLIST = {
        "now showing", "coming soon", "trailer", "beli tiket", "buy ticket",
        "jadwal", "schedule", "bioskop", "cinema", "home", "menu", "login",
        "register", "search", "cari", "see all", "lihat semua", "more",
        "cgv", "4dx", "imax", "screenx", "gold class", "starium", "soundx",
        "info", "about", "contact", "promo", "news", "faq", "syarat",
        "privacy", "copyright", "facebook", "instagram", "twitter",
    }

    def __init__(
        self,
        delay:       float = 1.0,
        timeout:     float = 30.0,
        max_retries: int   = 3,
        concurrency: int   = 5,
    ):
        self.delay       = delay
        self.timeout     = timeout
        self.max_retries = max_retries
        self.concurrency = concurrency
        self._sem        = asyncio.Semaphore(concurrency)
        self.logger      = logging.getLogger(self.__class__.__name__)

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> ScrapeResult:
        result = ScrapeResult(source=self.SOURCE)
        self.logger.info("🎬 CGV scraper start")

        async with CGVSession(delay=self.delay, timeout=self.timeout) as session:

            # 1. Cinema list
            try:
                cinemas = await self.scrape_cinemas(session)
                result.cinemas = cinemas
                self.logger.info(f"✅ {len(cinemas)} cinemas found")
            except Exception as e:
                msg = f"scrape_cinemas failed: {e}"
                self.logger.exception(msg)
                result.errors.append(msg)
                return result

            if not cinemas:
                self.logger.warning("CGV: 0 cinemas — periksa koneksi / struktur HTML")
                return result

            # 2. Movie list (concurrent, dibatasi semaphore)
            tasks = [
                self._scrape_movies_safe(session, cinema, i, len(cinemas))
                for i, cinema in enumerate(cinemas, 1)
            ]
            for movies in await asyncio.gather(*tasks):
                result.movies.extend(movies)

        self.logger.info(f"✅ CGV done: {result.cinema_count} cinemas, {result.movie_count} movies")
        return result

    # ── Cinema list ───────────────────────────────────────────────────────────

    async def scrape_cinemas(self, session: CGVSession) -> list[Cinema]:
        """
        GET /en/schedule/cinema → parse anchor tags ke /en/schedule/cinema/<id>.
        Probe sudah membuktikan 72 cinema IDs ada di HTML ini.
        """
        self.logger.info("CGV: fetching cinema list from /en/schedule/cinema...")
        resp = await session.get(CGV_SCHEDULE_PAGE)
        resp.raise_for_status()

        soup     = BeautifulSoup(resp.text, "lxml")
        cinemas  = self._parse_cinema_html(soup)

        # Jika HTML tidak cukup (perubahan struktur), coba /en/execute
        if not cinemas:
            self.logger.warning("CGV: HTML parse gagal, fallback ke /en/execute")
            cinemas = await self._fetch_cinemas_execute(session)

        return cinemas

    def _parse_cinema_html(self, soup: BeautifulSoup) -> list[Cinema]:
        """
        Parse anchor <a href='/en/schedule/cinema/XXX'> dari HTML.
        Struktur: kota (li > a[href=javascript]) → bioskop (ul > li > a[href=/cinema/XXX])
        """
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

            city = self._extract_city(link)
            full_url = href if href.startswith("http") else f"{CGV_BASE}{href}"
            full_name = f"CGV {name}" if not name.upper().startswith("CGV") else name

            cinemas.append(Cinema(
                name=full_name,
                chain=self.CHAIN,
                city=city,
                source=self.SOURCE,
                external_id=ext_id,
                booking_url=full_url,
            ))

        self.logger.info(f"CGV: {len(cinemas)} cinemas parsed from HTML")
        return cinemas

    def _extract_city(self, link) -> str:
        """Ekstrak nama kota dari parent list item."""
        try:
            # Struktur: <li.kota><a href=javascript>NamaKota</a><ul><li><a href=/cinema/XXX>
            li      = link.find_parent("li")
            ul      = li.find_parent("ul") if li else None
            city_li = ul.find_parent("li") if ul else None
            if city_li:
                city_a = city_li.find("a", href=re.compile(r"javascript|#|^$"))
                if city_a:
                    return normalize_city(clean_text(city_a.get_text()))
                heading = city_li.find(["strong", "span", "h2", "h3", "h4"])
                if heading:
                    return normalize_city(clean_text(heading.get_text()))
        except Exception:
            pass
        return ""

    async def _fetch_cinemas_execute(self, session: CGVSession) -> list[Cinema]:
        """
        Fallback: POST /en/execute untuk mendapat cinema list sebagai JSON.
        Perlu eksperimen action name yang benar — dicoba beberapa opsi.
        """
        actions = [
            {"action": "get_cinema_list"},
            {"action": "cinema_list"},
            {"action": "get_area_cinema"},
            {"action": "all_cinema"},
        ]
        for payload in actions:
            try:
                r = await session.post_ajax(CGV_EXECUTE_URL, payload)
                if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    data  = r.json()
                    items = data if isinstance(data, list) else data.get("data", data.get("cinemas", []))
                    if items:
                        self.logger.info(f"CGV /en/execute: {len(items)} cinemas (payload={payload})")
                        return [c for c in (self._dict_to_cinema(i) for i in items) if c]
            except Exception as e:
                self.logger.debug(f"CGV execute {payload}: {e}")
        return []

    def _dict_to_cinema(self, item: dict) -> Cinema | None:
        name = clean_text(item.get("name") or item.get("cinema_name") or "")
        if not name:
            return None
        ext_id = str(item.get("id") or item.get("cinema_id") or "")
        city   = normalize_city(item.get("city") or item.get("area") or "")
        href   = item.get("url") or f"/en/schedule/cinema/{ext_id}"
        url    = href if href.startswith("http") else f"{CGV_BASE}{href}"
        return Cinema(
            name=f"CGV {name}" if not name.upper().startswith("CGV") else name,
            chain=self.CHAIN,
            city=city,
            address=clean_text(item.get("address") or ""),
            source=self.SOURCE,
            external_id=ext_id,
            booking_url=url,
        )

    # ── Movie list ────────────────────────────────────────────────────────────

    async def _scrape_movies_safe(
        self,
        session:  CGVSession,
        cinema:   Cinema,
        index:    int,
        total:    int,
    ) -> list[CinemaMovie]:
        async with self._sem:
            try:
                movies = await self.scrape_movies(session, cinema)
                self.logger.info(f"[{index}/{total}] {cinema.name} → {len(movies)} movies")
                return movies
            except Exception as e:
                self.logger.warning(f"[{index}/{total}] {cinema.name} failed: {e}")
                return []

    async def scrape_movies(self, session: CGVSession, cinema: Cinema) -> list[CinemaMovie]:
        """
        POST /en/loader/home_movie_list
        Payload  : cinema_id=<3-digit id>
        Response : JSON array atau HTML snippet berisi daftar film

        Probe membuktikan semua payload lain (id, cinemaId, cinema) juga dikirim 
        ke endpoint ini — yang benar adalah 'cinema_id'.
        """
        if not cinema.external_id:
            return []

        payload = {"cinema_id": cinema.external_id}
        today   = date.today().strftime("%Y-%m-%d")

        for attempt, extra in enumerate([
            {},                            # Attempt 1: minimal
            {"date": today},               # Attempt 2: dengan tanggal
            {"schedule_date": today},      # Attempt 3: key berbeda
        ]):
            try:
                r = await session.post_ajax(
                    CGV_MOVIE_LOADER,
                    {**payload, **extra},
                    referer=f"{CGV_SCHEDULE_PAGE}/{cinema.external_id}",
                )

                if r.status_code != 200:
                    self.logger.debug(f"CGV movies {cinema.external_id}: HTTP {r.status_code}")
                    continue

                ct = r.headers.get("content-type", "")

                # Response JSON
                if "json" in ct or r.text.strip().startswith(("[", "{")):
                    movies = self._parse_movies_json(r.text, cinema)
                    if movies:
                        return movies

                # Response HTML snippet
                elif "html" in ct or r.text.strip().startswith("<"):
                    movies = self._parse_movies_html(r.text, cinema)
                    if movies:
                        return movies

                # Response plain text / unknown — coba keduanya
                else:
                    text = r.text.strip()
                    if text.startswith(("[", "{")):
                        movies = self._parse_movies_json(text, cinema)
                    else:
                        movies = self._parse_movies_html(text, cinema)
                    if movies:
                        return movies

            except Exception as e:
                self.logger.debug(f"CGV movies attempt {attempt+1} {cinema.name}: {e}")

        return []

    # ── Movie parsers ─────────────────────────────────────────────────────────

    def _parse_movies_json(self, raw: str, cinema: Cinema) -> list[CinemaMovie]:
        """Parse response JSON dari /en/loader/home_movie_list."""
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return []

        items = (
            data if isinstance(data, list)
            else data.get("data",
                 data.get("movies",
                 data.get("schedules",
                 data.get("result", []))))
        )
        if not isinstance(items, list):
            return []

        movies: list[CinemaMovie] = []
        seen:   set[str]          = set()

        for item in items:
            if not isinstance(item, dict):
                continue

            title = clean_text(
                item.get("title") or
                item.get("movie_title") or
                item.get("film_title") or
                item.get("name") or ""
            )
            if not self._valid_title(title) or title in seen:
                continue
            seen.add(title)

            # Format — bisa nested atau flat
            fmt_raw = (
                item.get("format") or
                item.get("type") or
                (item.get("formats") or [{}])[0].get("name", "2D")
                if isinstance(item.get("formats"), list) else "2D"
            )

            movies.append(CinemaMovie(
                cinema_id  = cinema.id,
                title      = title,
                genre      = clean_text(item.get("genre") or item.get("genres") or ""),
                duration   = extract_duration(str(item.get("duration") or item.get("runtime") or "")),
                age_rating = normalize_age_rating(item.get("rating") or item.get("age_rating") or ""),
                format     = normalize_format(str(fmt_raw)),
                source     = self.SOURCE,
                show_date  = date.today().isoformat(),
            ))

        return movies

    def _parse_movies_html(self, html: str, cinema: Cinema) -> list[CinemaMovie]:
        """
        Parse HTML snippet dari /en/loader/home_movie_list.
        CGV biasanya return partial HTML berisi card film.
        """
        movies: list[CinemaMovie] = []
        seen:   set[str]          = set()
        soup   = BeautifulSoup(html, "lxml")

        # Selector dari spesifik ke umum
        block_selectors = [
            "div.schedule-movie-item",
            "div[class*='movie-item']",
            "div[class*='schedule-movie']",
            "li[class*='movie']",
            ".movie-item",
            "article",
        ]

        for sel in block_selectors:
            blocks = soup.select(sel)
            if not blocks:
                continue

            for block in blocks:
                title_el = (
                    block.select_one("h3, h4, h2, .movie-title, [class*='title']")
                    or block.select_one("a[href*='/movie/'], a[href*='/film/']")
                )
                if not title_el:
                    continue

                title = clean_text(title_el.get_text())
                if not self._valid_title(title) or title in seen:
                    continue
                seen.add(title)

                rating_el = block.select_one("[class*='age'], [class*='rating'], .rating")
                dur_el    = block.select_one("[class*='duration'], [class*='runtime']")
                genre_el  = block.select_one("[class*='genre'], .genre")
                fmt_els   = block.select(
                    "[class*='format'], [class*='type'], .movie-type, "
                    "button[data-type], .badge, span[class*='format']"
                )

                formats = list({
                    normalize_format(clean_text(f.get_text()))
                    for f in fmt_els if clean_text(f.get_text())
                }) or ["2D"]

                for fmt in formats:
                    movies.append(CinemaMovie(
                        cinema_id  = cinema.id,
                        title      = title,
                        genre      = clean_text(genre_el.get_text()) if genre_el else "",
                        duration   = extract_duration(clean_text(dur_el.get_text())) if dur_el else "",
                        age_rating = normalize_age_rating(clean_text(rating_el.get_text())) if rating_el else "",
                        format     = fmt,
                        source     = self.SOURCE,
                        show_date  = date.today().isoformat(),
                    ))

            if movies:
                return movies

        # Fallback: ambil dari link film
        for a in soup.select("a[href*='/movie/'], a[href*='/film/']"):
            title = clean_text(a.get_text())
            if not self._valid_title(title) or title in seen:
                continue
            seen.add(title)
            movies.append(CinemaMovie(
                cinema_id = cinema.id,
                title     = title,
                format    = "2D",
                source    = self.SOURCE,
                show_date = date.today().isoformat(),
            ))

        return movies

    # ── Validation ────────────────────────────────────────────────────────────

    def _valid_title(self, title: str) -> bool:
        if not title or len(title) < 2 or len(title) > 120:
            return False
        if title.lower().strip() in self._BLACKLIST:
            return False
        if title.isdigit():
            return False
        if "http" in title or "@" in title:
            return False
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────────────────────────────────────

async def _test():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    scraper = CGVScraper(delay=1.0, concurrency=3)
    result  = await scraper.run()

    print(f"\n{'='*50}")
    print(f"Cinemas : {result.cinema_count}")
    print(f"Movies  : {result.movie_count}")
    print(f"Errors  : {len(result.errors)}")

    if result.cinemas:
        print(f"\nSample cinemas:")
        for c in result.cinemas[:5]:
            print(f"  {c.external_id} | {c.name} | {c.city}")

    if result.movies:
        print(f"\nSample movies:")
        for m in result.movies[:10]:
            print(f"  {m.title} | {m.format} | {m.duration} | cinema={m.cinema_id[:8]}")

    if result.errors:
        print(f"\nErrors:")
        for e in result.errors:
            print(f"  {e}")


if __name__ == "__main__":
    asyncio.run(_test())