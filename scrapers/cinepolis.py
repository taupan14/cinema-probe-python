"""
scrapers/cinepolis.py
=====================
SEMUA endpoint dan payload DIKONFIRMASI dari probe_cinepolis_v2.py

TEMUAN PROBE (ground truth):
─────────────────────────────────────────────────────────────────

1. loadCities
   POST /helper/CommonPageHelper.aspx/loadCities  payload: {}
   DataObject = HTML STRING (bukan JSON list!)
   Format: <a href='#' onclick="CommonPageHelper.SelCity('BAL');">Bali</a>

2. cinema-page.aspx
   GET via httpx → redirect ke /404.aspx (tidak bisa)
   Playwright → 91KB HTML, 57 SelCinema links ✓
   ASP.NET_SessionId cookie di-set saat ini → WAJIB disimpan untuk httpx calls

3. loadCinemaDetails
   POST /movie/cinemaDetail.aspx/loadCinemaDetails
   PAYLOAD BENAR: { 'pCinemaId' : 'uuid' }   ← prefix 'p', spasi di dalam key!
   payload {} → 500, payload {"cinemaId":...} → 500
   Response: DataObject.CinemaInfo.CinemaDet.{Title, SubTitle="Kota, Provinsi", FullAddress}

4. loadScheduleTimesByDate
   POST /movie/cinemaDetail.aspx/loadScheduleTimesByDate
   PAYLOAD BENAR:
     { 'pMovieId':'0', 'pCinemaId':'uuid', 'pScreenClass':'0', 'pSelDate':'DD/MM/YYYY' }
   Format tanggal DD/MM/YYYY  ← bukan YYYY-MM-DD!
   Response: DataObject.strScheduleTimeData = HTML accordion

   !! KRITIS (dikonfirmasi probe_schedule_per_cinema.py) !!
   Server MENGABAIKAN pCinemaId di POST payload!
   Server pakai ASP.NET Session untuk tahu cinema mana yang diminta.
   Session di-BIND ke cinema saat Playwright buka:
     cinemaDetail.aspx?id=CINEMA_UUID&title=SLUG
   → httpx POST dengan pCinemaId berbeda SELALU return data cinema pertama
   → SOLUSI: Playwright buka cinemaDetail per cinema, INTERCEPT response
     loadScheduleTimesByDate langsung (bukan POST ulang via httpx)

5. loadMovieDetails
   POST /movie/movieSchedule.aspx/loadMovieDetails
   PAYLOAD: { 'pMovieId' : 'uuid' }

6. CinemaMovie schema TIDAK punya field show_times.
   Showtimes disimpan sebagai Showtime objects terpisah.

7. Showtime schema fields: cinema_movie_id, cinema_id, show_date, show_time,
   format, source, movie_id(opt), studio_id(opt), ticket_price(opt)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import string
from datetime import date
from typing import Optional

from bs4 import BeautifulSoup

from models.schemas import Cinema, CinemaMovie, Showtime
from utils import (
    AsyncHTTPClient, clean_text, normalize_city,
    normalize_format, normalize_age_rating,
    extract_duration, today_iso,
)
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

BASE = "https://cinepolis.co.id"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type":     "application/json; charset=UTF-8",
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Accept-Language":  "id-ID,id;q=0.9,en;q=0.8",
    "Origin":           BASE,
    "X-Requested-With": "XMLHttpRequest",
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def decode(resp_text: str):
    """Decode double-encoded JSON: {"d": "{\"DataObject\": ...}"} → DataObject."""
    try:
        outer = json.loads(resp_text)
        inner = json.loads(outer.get("d", "{}"))
        return inner.get("DataObject")
    except Exception:
        return None


def today_ddmmyyyy() -> str:
    """Return tanggal hari ini dalam format DD/MM/YYYY (format Cinepolis)."""
    return date.today().strftime("%d/%m/%Y")


def slug_to_title(slug: str) -> str:
    return string.capwords(slug.replace("-", " "))


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

class CinepolisScraper(BaseScraper):
    SOURCE   = "cinepolis"
    CHAIN    = "Cinepolis"
    BASE_URL = BASE

    def __init__(self, **kwargs):
        super().__init__(concurrency=3, **kwargs)
        self._movie_cache: dict[str, dict] = {}
        self._movie_cache_lock = asyncio.Lock()
        # ASP.NET_SessionId dari Playwright — dipakai ulang di httpx calls
        self._session_cookie: str = ""
        self._pw = None
        self._pw_browser = None
        self._pw_context = None

    # ─────────────────────────────────────────────────────────────────────────
    # PLAYWRIGHT LIFECYCLE
    # ─────────────────────────────────────────────────────────────────────────

    async def _init_playwright(self) -> bool:
        if self._pw_browser:
            return True
        try:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._pw_browser = await self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            self._pw_context = await self._pw_browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
                locale="id-ID",
            )
            self.logger.info("Playwright initialized")
            return True
        except Exception as e:
            self.logger.error(f"Playwright init failed: {e}")
            return False

    async def _close_playwright(self) -> None:
        try:
            if self._pw_context:
                await self._pw_context.close()
                self._pw_context = None
            if self._pw_browser:
                await self._pw_browser.close()
                self._pw_browser = None
            if self._pw:
                await self._pw.stop()
                self._pw = None
        except Exception as e:
            self.logger.debug(f"Playwright close: {e}")

    async def run(self):
        try:
            return await super().run()
        finally:
            await self._close_playwright()

    def _headers_with_session(self, referer: str = "") -> dict:
        """Header lengkap + session cookie untuk httpx POST calls."""
        h = {**HEADERS}
        if referer:
            h["Referer"] = referer
        if self._session_cookie:
            h["Cookie"] = self._session_cookie
        return h

    # ─────────────────────────────────────────────────────────────────────────
    # SCRAPE CINEMAS
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_cinemas(self, client: AsyncHTTPClient) -> list[Cinema]:
        """
        1. Playwright buka cinema-page.aspx → session cookie + 57 cinema raw
        2. httpx POST loadCinemaDetails per cinema → nama, kota, alamat
        """
        raw_cinemas = await self._fetch_cinema_list_with_session(client)
        if not raw_cinemas:
            self.logger.error("Cinepolis: tidak ada cinema ditemukan")
            return []

        self.logger.info(f"Cinepolis: {len(raw_cinemas)} cinema raw, fetch detail ...")

        sem = asyncio.Semaphore(5)

        async def detail_task(raw):
            async with sem:
                return await self._fetch_cinema_detail(client, raw)

        results = await asyncio.gather(
            *[detail_task(r) for r in raw_cinemas],
            return_exceptions=True,
        )

        cinemas: list[Cinema] = []
        for raw, result in zip(raw_cinemas, results):
            if isinstance(result, Exception):
                self.logger.warning(f"detail {raw['name']}: {result}")
                cinemas.append(Cinema(
                    name=raw["name"], chain=self.CHAIN, city="",
                    source=self.SOURCE, external_id=raw["cinema_id"],
                    booking_url=raw["booking_url"],
                ))
            elif isinstance(result, Cinema):
                cinemas.append(result)

        self.logger.info(f"Cinepolis: {len(cinemas)} cinema final")
        return cinemas

    async def _fetch_cinema_list_with_session(
        self, client: AsyncHTTPClient
    ) -> list[dict]:
        """
        Playwright buka /cinema-page.aspx:
          - Simpan ASP.NET_SessionId cookie
          - Parse 57 SelCinema('uuid','slug') links
        """
        if not await self._init_playwright():
            return []

        items: list[dict] = []
        try:
            page = await self._pw_context.new_page()
            self.logger.info("Playwright: membuka /cinema-page.aspx ...")

            try:
                await page.goto(
                    f"{BASE}/cinema-page.aspx",
                    wait_until="domcontentloaded",
                    timeout=40_000,
                )
                await page.wait_for_timeout(4000)
            except Exception as e:
                self.logger.debug(f"cinema-page goto: {str(e)[:100]}")

            html = await page.content()
            self.logger.info(f"cinema-page.aspx: {len(html)} chars")

            # Simpan session cookie
            cookies = await self._pw_context.cookies()
            for c in cookies:
                if c["name"] == "ASP.NET_SessionId":
                    self._session_cookie = f"ASP.NET_SessionId={c['value']}"
                    self.logger.info(f"Session: {self._session_cookie}")
                    break

            await page.close()

            soup = BeautifulSoup(html, "lxml")
            seen: set[str] = set()

            for a in soup.select("a[onclick*='SelCinema']"):
                onclick = a.get("onclick", "")
                m = re.search(
                    r"SelCinema\(['\"]([^'\"]+)['\"],\s*['\"]([^'\"]+)['\"]",
                    onclick,
                )
                if not m:
                    continue
                cinema_id   = m.group(1)
                cinema_slug = m.group(2)
                if cinema_id in seen:
                    continue
                seen.add(cinema_id)

                cinema_name = clean_text(a.get_text()) or slug_to_title(cinema_slug)
                items.append({
                    "cinema_id":   cinema_id,
                    "cinema_slug": cinema_slug,
                    "name":        cinema_name,
                    "booking_url": (
                        f"{BASE}/movie/cinemaDetail.aspx"
                        f"?id={cinema_id}&title={cinema_slug}"
                    ),
                })

            self.logger.info(f"cinema-page.aspx: {len(items)} cinema")

        except Exception as e:
            self.logger.warning(f"_fetch_cinema_list_with_session: {e}")

        return items

    async def _fetch_cinema_detail(
        self, client: AsyncHTTPClient, raw: dict
    ) -> Cinema:
        """
        POST loadCinemaDetails.
        PAYLOAD: { 'pCinemaId' : 'uuid' }   ← dikonfirmasi probe
        City dari SubTitle: "Binjai, Sumatera Utara" → "Binjai"
        """
        cinema_id = raw["cinema_id"]
        try:
            resp = await client.post(
                f"{BASE}/movie/cinemaDetail.aspx/loadCinemaDetails",
                headers=self._headers_with_session(referer=raw["booking_url"]),
                json={"pCinemaId": cinema_id},
            )

            if resp.status_code != 200 or len(resp.text) < 100:
                self.logger.warning(
                    f"loadCinemaDetails [{raw['name']}]: "
                    f"status={resp.status_code} len={len(resp.text)}"
                )
                raise ValueError(f"response tidak valid")

            data = decode(resp.text)
            if not isinstance(data, dict):
                raise ValueError(f"DataObject bukan dict: {type(data).__name__}")

            det      = data.get("CinemaInfo", {}).get("CinemaDet", {})
            name     = clean_text(det.get("Title") or raw["name"])
            address  = clean_text(det.get("FullAddress") or "")
            subtitle = clean_text(det.get("SubTitle") or "")

            # "Binjai, Sumatera Utara" → city = "Binjai"
            city = normalize_city(subtitle.split(",")[0].strip()) if subtitle else ""

            # Koordinat (jika ada)
            lat, lng = 0.0, 0.0
            for key in ("Latitude", "latitude", "Lat"):
                try:
                    v = float(det.get(key) or 0)
                    if v:
                        lat = v
                        break
                except (TypeError, ValueError):
                    pass
            for key in ("Longitude", "longitude", "Lng"):
                try:
                    v = float(det.get(key) or 0)
                    if v:
                        lng = v
                        break
                except (TypeError, ValueError):
                    pass

            self.logger.debug(f"Cinema OK: {name} | {city} | {address[:50]}")
            return Cinema(
                name=name, chain=self.CHAIN, city=city,
                address=address, lat=lat, lng=lng,
                source=self.SOURCE, external_id=cinema_id,
                booking_url=raw["booking_url"],
            )

        except Exception as e:
            self.logger.warning(f"loadCinemaDetails [{raw['name']}]: {e}")
            return Cinema(
                name=raw["name"], chain=self.CHAIN, city="",
                source=self.SOURCE, external_id=cinema_id,
                booking_url=raw["booking_url"],
            )

    # ─────────────────────────────────────────────────────────────────────────
    # SCRAPE MOVIES + SHOWTIMES
    # ─────────────────────────────────────────────────────────────────────────

    async def scrape_movies(
        self, client: AsyncHTTPClient, cinema: Cinema
    ) -> tuple[list[CinemaMovie], list[Showtime]]:
        """
        Scrape jadwal film untuk satu cinema via Playwright intercept.

        KRITIS (dikonfirmasi probe_schedule_per_cinema.py):
          Server MENGABAIKAN pCinemaId di POST payload.
          Session ASP.NET di-bind ke cinema saat browser buka:
            cinemaDetail.aspx?id=CINEMA_UUID&title=SLUG
          → httpx POST untuk cinema berbeda selalu return data cinema yg sama.
          → WAJIB: Playwright per cinema, intercept response AJAX langsung.

        Serial (Semaphore 1): satu page aktif sekaligus agar session tidak
        tabrakan antar concurrent cinema.
        """
        if not cinema.external_id:
            return [], []

        # Semaphore serial: 1 Playwright page aktif sekaligus
        if not hasattr(self, "_pw_sem") or self._pw_sem is None:
            self._pw_sem = asyncio.Semaphore(1)

        async with self._pw_sem:
            schedule_html = await self._fetch_schedule_playwright(cinema)

        if len(schedule_html) < 100:
            self.logger.warning(f"[{cinema.name}] schedule kosong, skip")
            return [], []

        soup = BeautifulSoup(schedule_html, "lxml")
        cinema_movies, showtimes = await self._parse_cinema_schedule(
            soup, cinema, client
        )
        self.logger.info(
            f"[{cinema.name}]: {len(cinema_movies)} movies, {len(showtimes)} showtimes"
        )
        return cinema_movies, showtimes

    async def _fetch_schedule_playwright(self, cinema: Cinema) -> str:
        """
        Buka cinemaDetail.aspx via Playwright dan intercept
        loadScheduleTimesByDate response.

        Listener dipasang SEBELUM page.goto() → tidak ada race condition.
        Session ter-bind ke cinema yang benar karena URL mengandung ?id=UUID.
        """
        if not await self._init_playwright():
            return ""

        cinema_slug = self._extract_slug(cinema.booking_url)
        detail_url  = (
            f"{BASE}/movie/cinemaDetail.aspx"
            f"?id={cinema.external_id}&title={cinema_slug}"
        )
        schedule_html = ""

        try:
            page = await self._pw_context.new_page()

            # Listener SEBELUM goto — tidak ada race condition
            async def on_response(resp):
                nonlocal schedule_html
                if "loadScheduleTimesByDate" not in resp.url:
                    return
                if resp.status != 200:
                    return
                try:
                    body = await resp.text()
                    data = decode(body)
                    if isinstance(data, dict):
                        html = data.get("strScheduleTimeData") or ""
                        if len(html) > 100:
                            schedule_html = html
                            self.logger.info(
                                f"[{cinema.name}] schedule intercepted: {len(html)} chars"
                            )
                except Exception as ex:
                    self.logger.debug(f"on_response [{cinema.name}]: {ex}")

            page.on("response", on_response)

            try:
                await page.goto(
                    detail_url,
                    wait_until="domcontentloaded",
                    timeout=40_000,
                )
                # Tunggu AJAX loadScheduleTimesByDate dipanggil browser
                await page.wait_for_timeout(5000)
            except Exception as e:
                self.logger.debug(f"goto [{cinema.name}]: {str(e)[:120]}")

            await page.close()

        except Exception as e:
            self.logger.warning(f"_fetch_schedule_playwright [{cinema.name}]: {e}")

        return schedule_html

    # ─────────────────────────────────────────────────────────────────────────
    # PARSE SCHEDULE HTML
    # ─────────────────────────────────────────────────────────────────────────

    async def _parse_cinema_schedule(
        self,
        soup: BeautifulSoup,
        cinema: Cinema,
        client: AsyncHTTPClient,
    ) -> tuple[list[CinemaMovie], list[Showtime]]:
        """
        Parse strScheduleTimeData HTML.

        Struktur (dikonfirmasi probe):
          <div id='DivMovie_{uuid}'>
            <div class='head'>TUMBAL PROYEK</div>
            <div class='title mt-2'>HORROR <img/> 1h 46m</div>
            <div class='title mt-2'>2026 |D17| IDN</div>
            <div class='sch_wrap'>
              <span class='bld'>REGULAR 2D</span>
              <span class='regl'>RP 52,000</span>
              <div class='sch_date'>
                <a ...>11:30</a>
                <a ...>13:50</a>
              </div>
            </div>
          </div>

        PENTING: CinemaMovie tidak punya field show_times.
        Setiap jam tayang = satu Showtime object terpisah.
        """
        cinema_movies: list[CinemaMovie] = []
        showtimes: list[Showtime]        = []
        seen_cm: set[str]               = set()
        today = today_iso()

        movie_divs = soup.select("div[id^='DivMovie_']")
        self.logger.debug(f"[{cinema.name}] {len(movie_divs)} DivMovie blocks")

        if not movie_divs:
            self.logger.warning(f"[{cinema.name}] tidak ada DivMovie_ blocks")
            return [], []

        for div in movie_divs:
            movie_id = div.get("id", "").replace("DivMovie_", "").strip()
            if not movie_id:
                continue

            # ── Title ──
            head_el = div.select_one("div.head")
            title   = clean_text(head_el.get_text()) if head_el else ""
            if not title:
                continue

            # ── Genre + Duration dari div.title ──
            genre    = ""
            duration = ""
            for td in div.select("div.title"):
                text = clean_text(td.get_text())
                dur_m = re.search(
                    r'(\d+h\s*\d*m?|\d+\s*(?:menit|min)\b)',
                    text, re.IGNORECASE
                )
                if dur_m:
                    duration = dur_m.group(0).strip()
                    genre    = re.sub(
                        r'[\s•|\-,]+$', '',
                        text[:text.find(dur_m.group(0))].strip()
                    )
                    break

            # ── Age Rating ──
            age_rating = ""
            for td in div.select("div.title"):
                text = clean_text(td.get_text())
                rm = re.search(
                    r'\b(SU|R\s*13|D\s*13|D\s*17|13\+|17\+)\b',
                    text, re.IGNORECASE
                )
                if rm:
                    age_rating = normalize_age_rating(
                        rm.group(1).upper().replace(" ", "")
                    )
                    break

            # ── Showtimes per format ──
            # Tiap div.sch_wrap = satu format (REGULAR 2D, IMAX, dll)
            fmt_times_list: list[tuple[str, list[str], Optional[int]]] = []

            for wrap in div.select("div.sch_wrap"):
                bld = wrap.select_one("span.bld")
                fmt = normalize_format(clean_text(bld.get_text())) if bld else "2D"

                # Harga tiket — ambil angka PERTAMA saja dari teks
                # span.regl bisa multi-baris: "RP 47,000\nRP 72,000"
                # re.sub(r'[^\d]','') → "4700072000" (SALAH, tergabung)
                # Solusi: cari angka pertama dengan regex
                price: Optional[int] = None
                regl = wrap.select_one("span.regl")
                if regl:
                    pm = re.search(r'(\d[\d,\.]+)', regl.get_text())
                    if pm:
                        pc = re.sub(r'[,\.]', '', pm.group(1))
                        v  = int(pc) if pc.isdigit() else None
                        # Sanity check: harga tiket bioskop Indonesia 10rb-500rb
                        if v and 10_000 <= v <= 500_000:
                            price = v

                sch_date = wrap.select_one("div.sch_date")
                links    = sch_date.select("a") if sch_date else wrap.select("a")
                times    = self._extract_times(links)

                if times:
                    fmt_times_list.append((fmt, times, price))

            # Fallback: tidak ada sch_wrap
            if not fmt_times_list:
                times = self._extract_times(div.select("a"))
                if times:
                    fmt_times_list.append(("2D", times, None))
            if not fmt_times_list:
                fmt_times_list = [("2D", [], None)]

            # ── Enrich dari detail movie API ──
            detail = await self._get_movie_detail(client, movie_id)
            if not genre:
                genre = clean_text(detail.get("genre") or "")
            if not duration:
                dur_raw = str(detail.get("duration") or "").strip()
                duration = (
                    f"{dur_raw} menit" if dur_raw.isdigit()
                    else extract_duration(dur_raw)
                )
            if not age_rating:
                age_rating = normalize_age_rating(detail.get("age_rating") or "")

            # ── Buat CinemaMovie + Showtime ──
            for fmt, show_times, price in fmt_times_list:
                cm_key = f"{cinema.id}|{title}|{fmt}|{today}"

                if cm_key in seen_cm:
                    # Sudah ada → tambah showtime ke cm yang existing
                    existing_cm = next(
                        (c for c in cinema_movies
                         if c.title == title and c.format == fmt),
                        None,
                    )
                    if existing_cm:
                        for t in show_times:
                            showtimes.append(Showtime(
                                cinema_movie_id=existing_cm.id,
                                cinema_id=cinema.id,
                                show_date=today,
                                show_time=t,
                                format=fmt,
                                source=self.SOURCE,
                                ticket_price=price,
                            ))
                    continue

                seen_cm.add(cm_key)

                cm = CinemaMovie(
                    cinema_id  = cinema.id,
                    title      = title,
                    source     = self.SOURCE,
                    movie_code = movie_id,
                    genre      = genre,
                    duration   = duration,
                    age_rating = age_rating,
                    format     = fmt,
                    show_date  = today,
                )
                cm._raw_detail = {
                    "movie_code":  movie_id,
                    "title":       title,
                    "overview":    clean_text(detail.get("synopsis") or ""),
                    "runtime":     self._duration_to_minutes(duration),
                    "poster_path": detail.get("poster") or "",
                    "trailer_key": detail.get("trailer") or "",
                    "cast_raw":    clean_text(detail.get("cast") or ""),
                    "director":    clean_text(detail.get("director") or ""),
                }
                cinema_movies.append(cm)

                for t in show_times:
                    showtimes.append(Showtime(
                        cinema_movie_id=cm.id,
                        cinema_id=cinema.id,
                        show_date=today,
                        show_time=t,
                        format=fmt,
                        source=self.SOURCE,
                        ticket_price=price,
                    ))

        return cinema_movies, showtimes

    # ─────────────────────────────────────────────────────────────────────────
    # MOVIE DETAIL
    # ─────────────────────────────────────────────────────────────────────────

    async def _get_movie_detail(
        self, client: AsyncHTTPClient, movie_id: str
    ) -> dict:
        """
        POST /movie/movieSchedule.aspx/loadMovieDetails
        PAYLOAD: { 'pMovieId' : 'uuid' }
        Di-cache per movie_id untuk efisiensi.
        """
        async with self._movie_cache_lock:
            if movie_id in self._movie_cache:
                return self._movie_cache[movie_id]

        detail: dict = {}
        try:
            resp = await client.post(
                f"{BASE}/movie/movieSchedule.aspx/loadMovieDetails",
                headers=self._headers_with_session(referer=f"{BASE}/"),
                json={"pMovieId": movie_id},
            )
            if resp.status_code == 200 and len(resp.text) > 100:
                data = decode(resp.text)
                if isinstance(data, dict):
                    html_str = data.get("strDetail") or ""
                    if html_str:
                        detail = self._parse_movie_detail_html(html_str)
        except Exception as e:
            self.logger.debug(f"loadMovieDetails {movie_id}: {e}")

        async with self._movie_cache_lock:
            self._movie_cache[movie_id] = detail

        return detail

    def _parse_movie_detail_html(self, html: str) -> dict:
        """
        Parse HTML strDetail.
        Format: <h1>JUDUL</h1><p>GENRE • DURASI MENIT • RATING</p>
        """
        soup   = BeautifulSoup(html, "lxml")
        detail = {}

        h1 = soup.select_one("h1")
        if h1:
            detail["title"] = clean_text(h1.get_text())

        m = re.search(r"viewMovTrailer\(['\"]([^'\"]+)['\"]", html)
        if m:
            detail["trailer"] = m.group(1)

        for img in soup.select("img"):
            src = img.get("src", "")
            if "cms2.cinepolis.co.id" in src and "/MOV/" in src:
                detail["poster"] = src
                break

        # Genre • Durasi • Rating
        for p in soup.select("p"):
            text = clean_text(p.get_text())
            if "•" in text or re.search(r'\d+\s*(menit|min)\b', text, re.I):
                parts = [x.strip() for x in text.split("•")]
                if len(parts) >= 1:
                    detail["genre"] = parts[0]
                if len(parts) >= 2:
                    dm = re.search(r'(\d+)', parts[1])
                    if dm:
                        detail["duration"] = dm.group(1)
                if len(parts) >= 3:
                    detail["age_rating"] = parts[2]
                break

        for sel in [".synopsis", "p.synopsis", "[class*='synopsis']", ".overview"]:
            el = soup.select_one(sel)
            if el:
                detail["synopsis"] = clean_text(el.get_text())
                break

        for label in ["Director", "Direktor", "Sutradara"]:
            m2 = re.search(rf"{label}[:\s]+([^\n<]+)", html, re.IGNORECASE)
            if m2:
                detail["director"] = clean_text(m2.group(1))
                break

        for label in ["Cast", "Pemain", "Stars", "Bintang"]:
            m2 = re.search(rf"{label}[:\s]+([^\n<]+)", html, re.IGNORECASE)
            if m2:
                detail["cast"] = clean_text(m2.group(1))
                break

        return detail

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_times(links) -> list[str]:
        """Extract jam tayang dari list <a> elements. Format: HH:MM dari teks link."""
        times = []
        seen  = set()
        for a in links:
            text = clean_text(a.get_text())
            if re.match(r'^\d{1,2}:\d{2}$', text):
                h, mn = text.split(":")
                normalized = f"{int(h):02d}:{mn}"
                if normalized not in seen:
                    seen.add(normalized)
                    times.append(normalized)
        return sorted(times)

    @staticmethod
    def _duration_to_minutes(duration_str: str) -> Optional[int]:
        """Konversi string durasi ke integer menit. "1h 46m" → 106."""
        if not duration_str:
            return None
        s = duration_str.lower().strip()
        m = re.search(r'(\d+)\s*h\s*(\d+)?\s*m?', s)
        if m:
            return int(m.group(1)) * 60 + int(m.group(2) or 0)
        m = re.search(r'(\d+)\s*(?:menit|min)\b', s)
        if m:
            return int(m.group(1))
        m = re.match(r'^(\d{2,3})$', s)
        if m:
            v = int(m.group(1))
            if 40 <= v <= 300:
                return v
        return None

    @staticmethod
    def _extract_slug(booking_url: str) -> str:
        m = re.search(r'title=([^&]+)', booking_url or "")
        return m.group(1) if m else ""