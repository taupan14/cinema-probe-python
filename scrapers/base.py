"""
scrapers/base.py
Abstract base class untuk semua scraper.

PERUBAHAN:
- scrape_movies() boleh return list[CinemaMovie] ATAU tuple(list[CinemaMovie], list[Showtime])
  Backward-compatible: scraper CGV/Cinepolis yang masih return list tetap bekerja.
- ScrapeResult ditambah field showtimes untuk menampung Showtime dari XXI
- _scrape_movies_safe() unpack tuple jika return value adalah tuple
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from models import ScrapeResult, Showtime
from utils import AsyncHTTPClient

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    SOURCE: str = ""
    CHAIN: str = ""
    BASE_URL: str = ""

    def __init__(
        self,
        delay: float = 1.5,
        timeout: float = 30.0,
        max_retries: int = 3,
        concurrency: int = 5,
    ):
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.concurrency = concurrency
        self.semaphore = asyncio.Semaphore(concurrency)
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def scrape_cinemas(self, client: AsyncHTTPClient) -> list:
        """Scrape semua bioskop."""
        ...

    @abstractmethod
    async def scrape_movies(self, client: AsyncHTTPClient, cinema) -> list | tuple:
        """
        Scrape film per bioskop.

        Return boleh berupa:
          - list[CinemaMovie]                          (scraper lama: CGV, Cinepolis)
          - tuple(list[CinemaMovie], list[Showtime])   (XXI)
        """
        ...

    async def _scrape_movies_safe(
        self,
        client: AsyncHTTPClient,
        cinema,
        index: int,
        total: int,
    ) -> tuple[list, list]:
        """
        Wrapper scrape_movies dengan semaphore + error handling.

        Selalu return tuple(movies, showtimes) agar run() konsisten.
        Scraper lama yang return list akan di-wrap otomatis jadi (list, []).
        """
        async with self.semaphore:
            try:
                result = await self.scrape_movies(client, cinema)

                # Normalize return value → selalu tuple
                if isinstance(result, tuple):
                    movies, showtimes = result
                else:
                    movies, showtimes = result, []

                self.logger.info(
                    f"[{index}/{total}] {cinema.name} → "
                    f"{len(movies)} movies, {len(showtimes)} showtimes"
                )
                return movies, showtimes

            except Exception as e:
                self.logger.warning(
                    f"[{index}/{total}] scrape_movies failed for "
                    f"{cinema.name}: {type(e).__name__}: {e}"
                )
                return [], []
            finally:
                if self.delay > 0:
                    await asyncio.sleep(self.delay)

    async def run(self) -> ScrapeResult:
        """Main entry point scraper."""
        result = ScrapeResult(source=self.SOURCE)
        self.logger.info(f"🎬 Starting scrape: {self.SOURCE} ({self.CHAIN})")

        async with AsyncHTTPClient(
            base_url=self.BASE_URL,
            delay=self.delay,
            timeout=self.timeout,
            max_retries=self.max_retries,
        ) as client:

            # ── Scrape cinemas ────────────────────────────────────────────
            try:
                cinemas = await self.scrape_cinemas(client)
                result.cinemas = cinemas
                self.logger.info(
                    f"✅ Found {len(cinemas)} cinemas for {self.SOURCE}"
                )
                if not cinemas:
                    self.logger.warning(
                        f"⚠️  {self.SOURCE}: Tidak ada cinema ditemukan. "
                        f"Kemungkinan: (1) website diblokir dari IP ini, "
                        f"(2) struktur HTML berubah, atau (3) endpoint API tidak aktif."
                    )
                    return result
            except Exception as e:
                msg = f"scrape_cinemas failed for {self.SOURCE}: {type(e).__name__}: {e}"
                self.logger.exception(msg)
                result.errors.append(msg)
                return result

            # ── Scrape movies (concurrent) ────────────────────────────────
            tasks = [
                self._scrape_movies_safe(
                    client=client,
                    cinema=cinema,
                    index=i,
                    total=len(cinemas),
                )
                for i, cinema in enumerate(cinemas, start=1)
            ]

            all_results = await asyncio.gather(*tasks, return_exceptions=False)

            for movies, showtimes in all_results:
                if movies:
                    result.movies.extend(movies)
                if showtimes:
                    result.showtimes.extend(showtimes)

        self.logger.info(
            f"✅ Done {self.SOURCE}: "
            f"{result.cinema_count} cinemas, "
            f"{result.movie_count} movies, "
            f"{result.showtime_count} showtimes"
        )
        return result