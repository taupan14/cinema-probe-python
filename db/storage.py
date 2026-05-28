"""
db/storage.py

PERUBAHAN v3:
- upsert_movies(): return tetap int (backward-compatible, CGV tidak terpengaruh)
  movie_id_cache disimpan ke self._last_movie_id_cache (instance variable)
- upsert_showtimes(): baca movie_id_cache dari instance variable, tidak perlu
  parameter tambahan — CGV yang tidak pakai showtimes tidak perlu diubah
- upsert_movies(): hapus kolom "show_times" yang tidak ada di tabel DB
- upsert_showtimes(): tambah studio_id, ticket_price, backfill movie_id otomatis
"""
from __future__ import annotations
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional
import httpx

from models import Cinema, CinemaMovie, Showtime, ScrapeResult
from utils import save_json, save_csv

logger = logging.getLogger(__name__)

BATCH = 100


class SupabaseStorage:
    """Simpan data ke Supabase menggunakan REST API langsung."""

    def __init__(self, url: str, service_key: str):
        self.url = url.rstrip("/")
        self.headers = {
            "apikey":        service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates,return=representation",
        }
        # Internal cache — diisi oleh upsert_movies(), dibaca oleh upsert_showtimes()
        # Tidak perlu diakses dari luar class
        self._last_movie_id_cache: dict[str, Optional[int]] = {}
        self._last_cinema_movies:  list[CinemaMovie] = []
        # Map (cinema_id, movie_id, format, show_date) → db UUID cinema_movies
        self._cm_key_to_db_uuid: dict[tuple, Optional[str]] = {}

    # ── Cinemas ───────────────────────────────────────────────────────────────

    async def upsert_cinemas(self, cinemas: list[Cinema]) -> int:
        """Upsert cinemas by (source, external_id). Return jumlah row."""
        if not cinemas:
            return 0
        rows = []
        for c in cinemas:
            d = c.to_db_dict()
            # Strip ?city_id=N yang kita pakai sebagai metadata internal
            d["booking_url"] = (d.get("booking_url") or "").split("?")[0]
            rows.append(d)
        return await self._upsert("cinemas", rows, on_conflict="source,external_id")

    async def get_cinema_map(self, source: str) -> dict[str, str]:
        """Return {external_id → supabase UUID} untuk source tertentu."""
        mapping: dict[str, str] = {}
        try:
            async with httpx.AsyncClient(verify=False, timeout=20) as client:
                resp = await client.get(
                    f"{self.url}/rest/v1/cinemas",
                    headers=self.headers,
                    params={"source": f"eq.{source}", "select": "id,external_id"},
                )
                if resp.status_code == 200:
                    for row in resp.json():
                        mapping[row["external_id"]] = row["id"]
        except Exception as e:
            logger.error(f"get_cinema_map failed: {e}")
        return mapping

    async def get_cinema_ids(self, source: str) -> dict[str, str]:
        """Alias get_cinema_map — dipanggil dari main.py."""
        return await self.get_cinema_map(source)

    # ── Movies (lookup dulu, insert jika perlu) ───────────────────────────────

    async def _lookup_movie_id(
        self, client: httpx.AsyncClient, title: str
    ) -> Optional[int]:
        """Cari movies.id by title (case-insensitive)."""
        try:
            resp = await client.get(
                f"{self.url}/rest/v1/movies",
                headers=self.headers,
                params={"title": f"ilike.{title}", "select": "id", "limit": "1"},
            )
            if resp.status_code == 200 and resp.json():
                return resp.json()[0]["id"]
            if ":" in title:
                main = title.split(":")[0].strip()
                resp2 = await client.get(
                    f"{self.url}/rest/v1/movies",
                    headers=self.headers,
                    params={"title": f"ilike.*{main}*", "select": "id", "limit": "1"},
                )
                if resp2.status_code == 200 and resp2.json():
                    return resp2.json()[0]["id"]
        except Exception as e:
            logger.warning(f"lookup_movie_id '{title}': {e}")
        return None

    async def _insert_movie(
        self, client: httpx.AsyncClient, detail: dict
    ) -> Optional[int]:
        """Insert film baru dengan tmdb_id negatif sebagai placeholder."""
        title      = detail.get("title") or ""
        movie_code = detail.get("movie_code") or ""
        if not title or not movie_code:
            return None

        existing = await self._lookup_movie_id(client, title)
        if existing:
            return existing

        fake_tmdb_id = -(abs(hash(movie_code)) % 999_999_999)
        try:
            resp = await client.post(
                f"{self.url}/rest/v1/movies",
                headers={**self.headers, "Prefer": "return=representation"},
                json={
                    "tmdb_id":        fake_tmdb_id,
                    "title":          title,
                    "original_title": title,
                    "overview":       detail.get("overview") or "",
                    "poster_path":    detail.get("poster_path") or "",
                    "trailer_key":    detail.get("trailer_key") or "",
                    "runtime":        detail.get("runtime"),
                    "status":         "Released",
                    "release_date":   datetime.today().strftime("%Y-%m-%d"),
                },
            )
            if resp.status_code in (200, 201) and resp.json():
                movie_id = resp.json()[0]["id"]
                logger.info(f"Inserted movie '{title}' id={movie_id}")
                return movie_id
        except Exception as e:
            logger.warning(f"insert_movie '{title}': {e}")
            return await self._lookup_movie_id(client, title)
        return None

    async def _insert_cast_crew(
        self, client: httpx.AsyncClient, movie_id: int, detail: dict
    ) -> None:
        """Insert movie_cast dan movie_crew jika belum ada."""
        chk = await client.get(
            f"{self.url}/rest/v1/movie_cast",
            headers=self.headers,
            params={"movie_id": f"eq.{movie_id}", "select": "id", "limit": "1"},
        )
        if chk.status_code == 200 and not chk.json():
            cast_names = [
                n.strip()
                for n in (detail.get("cast_raw") or "").split(",")
                if n.strip()
            ]
            if cast_names:
                cast_rows = [
                    {
                        "movie_id":    movie_id,
                        "person_id":   -(abs(hash(n)) % 999_999),
                        "name":        n,
                        "order_index": i,
                        "job":         "Actor",
                        "department":  "Acting",
                    }
                    for i, n in enumerate(cast_names[:20])
                ]
                try:
                    await client.post(
                        f"{self.url}/rest/v1/movie_cast",
                        headers={**self.headers, "Prefer": "return=minimal"},
                        json=cast_rows,
                    )
                except Exception as e:
                    logger.warning(f"insert cast movie_id={movie_id}: {e}")

        crew_rows = []
        for field_key, job, dept in [
            ("director", "Director",   "Directing"),
            ("producer", "Producer",   "Production"),
            ("writer",   "Screenplay", "Writing"),
        ]:
            for name in [
                n.strip()
                for n in (detail.get(field_key) or "").split(",")
                if n.strip()
            ]:
                crew_rows.append({
                    "movie_id":   movie_id,
                    "person_id":  -(abs(hash(f"{name}{job}")) % 999_999),
                    "name":       name,
                    "job":        job,
                    "department": dept,
                })
        if crew_rows:
            chk2 = await client.get(
                f"{self.url}/rest/v1/movie_crew",
                headers=self.headers,
                params={"movie_id": f"eq.{movie_id}", "select": "movie_id", "limit": "1"},
            )
            if chk2.status_code == 200 and not chk2.json():
                try:
                    await client.post(
                        f"{self.url}/rest/v1/movie_crew",
                        headers={**self.headers, "Prefer": "return=minimal"},
                        json=crew_rows,
                    )
                except Exception as e:
                    logger.warning(f"insert crew movie_id={movie_id}: {e}")

    async def _set_category(
        self, client: httpx.AsyncClient, movie_id: int,
        category: str, sort_order: int = 0
    ) -> None:
        try:
            await client.post(
                f"{self.url}/rest/v1/movie_categories",
                headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "movie_id,category,region"},
                json={
                    "movie_id":   movie_id,
                    "category":   category,
                    "sort_order": sort_order,
                    "region":     "ID",
                },
            )
        except Exception as e:
            logger.warning(f"set_category movie_id={movie_id} {category}: {e}")

    # ── Cinema Movies ─────────────────────────────────────────────────────────

    async def upsert_movies(
        self,
        movies: list[CinemaMovie],
        local_to_db: dict[str, str],
    ) -> int:
        """
        Per CinemaMovie:
          1. Lookup movie_id di tabel movies by title
          2. Insert jika belum ada
          3. Insert cast/crew + set category now_playing
          4. Upsert ke cinema_movies

        Return: int — TIDAK BERUBAH, backward-compatible dengan CGV dan scraper lain.

        Side effect: menyimpan movie_id_cache ke self._last_movie_id_cache
        dan movies ke self._last_cinema_movies agar upsert_showtimes() bisa
        backfill movie_id tanpa perubahan signature apapun.
        CGV yang tidak memanggil upsert_showtimes() tidak terpengaruh sama sekali.
        """
        # Reset cache
        self._last_movie_id_cache = {}
        self._last_cinema_movies  = list(movies)
        self._cm_key_to_db_uuid   = {}

        if not movies:
            return 0

        total = 0
        movie_id_cache: dict[str, Optional[int]] = {}

        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            cm_rows = []
            for m in movies:
                cache_key = getattr(m, "movie_code", "") or m.title
                if cache_key not in movie_id_cache:
                    detail   = getattr(m, "_raw_detail", {})
                    movie_id = await self._lookup_movie_id(client, m.title)
                    if not movie_id and detail:
                        movie_id = await self._insert_movie(client, detail)
                    movie_id_cache[cache_key] = movie_id

                    if movie_id:
                        await self._insert_cast_crew(client, movie_id, detail)
                        await self._set_category(client, movie_id, "now_playing")

                movie_id = movie_id_cache.get(cache_key)
                if not movie_id:
                    logger.debug(f"Skip cinema_movie '{m.title}': movie_id tidak ditemukan")
                    continue

                db_cinema_id = local_to_db.get(m.cinema_id, m.cinema_id)
                cm_rows.append({
                    "cinema_id":  db_cinema_id,
                    "movie_id":   movie_id,
                    "movie_code": getattr(m, "movie_code", "") or "",
                    "title":      m.title,
                    "genre":      getattr(m, "genre", "") or "",
                    "duration":   getattr(m, "duration", "") or "",
                    "age_rating": getattr(m, "age_rating", "") or "",
                    "format":     getattr(m, "format", "2D") or "2D",
                    "show_date":  getattr(m, "show_date", "") or "",
                    # "show_times" DIHAPUS — kolom ini tidak ada di tabel cinema_movies
                    "source":     m.source,
                })

            for i in range(0, len(cm_rows), BATCH):
                batch = cm_rows[i : i + BATCH]
                try:
                    resp = await client.post(
                        f"{self.url}/rest/v1/cinema_movies",
                        json=batch,
                        # return=representation agar dapat UUID dari DB
                        headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=representation"},
                        params={"on_conflict": "cinema_id,movie_id,format,show_date"},
                    )
                    if resp.status_code in (200, 201):
                        total += len(batch)
                        # Simpan map: (cinema_id,movie_id,format,show_date) → db UUID
                        for db_row in resp.json():
                            key = (
                                db_row.get("cinema_id"),
                                db_row.get("movie_id"),
                                db_row.get("format"),
                                db_row.get("show_date"),
                            )
                            self._cm_key_to_db_uuid[key] = db_row.get("id")
                    else:
                        logger.error(
                            f"upsert cinema_movies batch {i}: "
                            f"{resp.status_code} {resp.text[:200]}"
                        )
                except Exception as e:
                    logger.error(f"upsert cinema_movies batch {i}: {e}")

        # Simpan ke instance variable untuk dipakai upsert_showtimes()
        self._last_movie_id_cache = movie_id_cache
        return total

    # ── Showtimes ─────────────────────────────────────────────────────────────

    async def upsert_showtimes(
        self,
        showtimes: list[Showtime],
        local_to_db: dict[str, str],
    ) -> int:
        """
        Insert showtimes ke tabel showtimes.
        Dipanggil SETELAH upsert_movies() — movie_id di-backfill otomatis
        dari self._last_movie_id_cache yang diisi upsert_movies().

        Signature tidak berubah dari versi sebelumnya.
        CGV tidak memanggil method ini jadi tidak terpengaruh.
        """
        if not showtimes:
            return 0

        # Bangun map: CinemaMovie.id → movie_id
        # Tiap Showtime.cinema_movie_id = CinemaMovie.id (UUID lokal)
        cm_to_movie: dict[str, Optional[int]] = {}
        for cm in self._last_cinema_movies:
            cache_key = getattr(cm, "movie_code", "") or cm.title
            cm_to_movie[cm.id] = self._last_movie_id_cache.get(cache_key)

        rows = []
        for st in showtimes:
            db_cinema_id = local_to_db.get(st.cinema_id, st.cinema_id)
            movie_id     = st.movie_id or cm_to_movie.get(st.cinema_movie_id)

            # Resolve cinema_movie_id: pakai UUID dari DB, bukan UUID lokal
            # UUID lokal tidak ada di tabel cinema_movies → FK violation
            db_cm_uuid = self._cm_key_to_db_uuid.get((
                db_cinema_id,
                movie_id,
                st.format,
                st.show_date,
            ))

            rows.append({
                "cinema_movie_id": db_cm_uuid,   # None jika tidak ditemukan → kolom nullable
                "cinema_id":       db_cinema_id,
                "movie_id":        movie_id,
                "show_date":       st.show_date,
                "show_time":       st.show_time,
                "format":          st.format,
                "studio_id":       st.studio_id,
                "ticket_price":    st.ticket_price,
                "source":          st.source,
            })

        # Dedup dalam rows sebelum batch — ON CONFLICT tidak bisa handle
        # duplikat dalam satu batch yang sama (error 21000)
        seen_keys: set[tuple] = set()
        deduped_rows = []
        for row in rows:
            key = (
                row.get("cinema_id"),
                row.get("movie_id"),
                row.get("format"),
                row.get("show_date"),
                row.get("show_time"),
            )
            if key not in seen_keys and None not in key:
                seen_keys.add(key)
                deduped_rows.append(row)

        logger.info(f"showtimes: {len(rows)} rows -> {len(deduped_rows)} setelah dedup")

        total = 0
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            for i in range(0, len(deduped_rows), BATCH):
                batch = deduped_rows[i : i + BATCH]
                try:
                    resp = await client.post(
                        f"{self.url}/rest/v1/showtimes",
                        json=batch,
                        headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                        params={"on_conflict": "cinema_id,movie_id,format,show_date,show_time"},
                    )
                    if resp.status_code in (200, 201):
                        total += len(batch)
                    else:
                        logger.error(
                            f"upsert showtimes batch {i}: "
                            f"{resp.status_code} {resp.text[:200]}"
                        )
                except Exception as e:
                    logger.error(f"upsert showtimes batch {i}: {e}")

        return total

    # ── Upcoming ──────────────────────────────────────────────────────────────

    async def upsert_upcoming(self, upcoming_films: list[dict]) -> int:
        """Insert upcoming movies ke movies + movie_categories."""
        if not upcoming_films:
            return 0
        saved = 0
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            for i, film in enumerate(upcoming_films):
                title      = (film.get("title") or "").strip()
                movie_code = film.get("dc21_parent_movie_code") or ""
                if not title:
                    continue
                dur_raw = str(film.get("duration") or "").strip()
                detail = {
                    "movie_code":  movie_code,
                    "title":       title,
                    "overview":    (film.get("synopsis") or "").strip(),
                    "runtime":     int(dur_raw) if dur_raw.isdigit() else None,
                    "poster_path": film.get("movie_image") or "",
                    "trailer_key": film.get("trailer") or "",
                    "cast_raw":    (film.get("player") or "").strip(),
                    "director":    (film.get("director") or "").strip(),
                    "producer":    (film.get("producer") or "").strip(),
                    "writer":      (film.get("writer") or "").strip(),
                }
                movie_id = await self._lookup_movie_id(client, title)
                if not movie_id:
                    movie_id = await self._insert_movie(client, detail)
                if movie_id:
                    await self._insert_cast_crew(client, movie_id, detail)
                    await self._set_category(client, movie_id, "upcoming", sort_order=i)
                    saved += 1
        return saved

    # ── Internal helper ───────────────────────────────────────────────────────

    async def _upsert(
        self, table: str, rows: list[dict], on_conflict: str = ""
    ) -> int:
        if not rows:
            return 0
        headers = dict(self.headers)
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
        inserted = 0
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            for i in range(0, len(rows), BATCH):
                batch = rows[i : i + BATCH]
                params = {"on_conflict": on_conflict} if on_conflict else {}
                try:
                    resp = await client.post(
                        f"{self.url}/rest/v1/{table}",
                        json=batch,
                        headers=headers,
                        params=params,
                    )
                    if resp.status_code in (200, 201):
                        inserted += len(batch)
                    else:
                        logger.error(
                            f"Supabase upsert {table} batch {i}: "
                            f"{resp.status_code} {resp.text[:200]}"
                        )
                except Exception as e:
                    logger.error(f"Supabase batch {i} failed: {e}")
        return inserted


# ── Local storage ─────────────────────────────────────────────────────────────

class LocalStorage:
    def __init__(self, output_dir: str = "./output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def save_result(self, result: ScrapeResult, prefix: str = "") -> dict[str, str]:
        ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
        source = result.source or "all"
        name   = f"{prefix}{source}_{ts}" if prefix else f"{source}_{ts}"
        paths  = {}

        json_path = self.output_dir / f"{name}.json"
        save_json({
            "source":         result.source,
            "scraped_at":     datetime.utcnow().isoformat(),
            "cinema_count":   result.cinema_count,
            "movie_count":    result.movie_count,
            "showtime_count": result.showtime_count,
            "errors":         result.errors,
            "cinemas":        [c.to_dict() for c in result.cinemas],
            "movies":         [m.to_dict() for m in result.movies],
        }, json_path)
        paths["json"] = str(json_path)

        if result.cinemas:
            p = self.output_dir / f"{name}_cinemas.csv"
            save_csv([c.to_dict() for c in result.cinemas], p)
            paths["cinemas_csv"] = str(p)

        if result.movies:
            p = self.output_dir / f"{name}_movies.csv"
            save_csv([m.to_dict() for m in result.movies], p)
            paths["movies_csv"] = str(p)

        logger.info(f"Saved: {paths}")
        return paths

    def save_combined(self, results: list[ScrapeResult]) -> dict[str, str]:
        combined = ScrapeResult(source="all")
        for r in results:
            combined.cinemas.extend(r.cinemas)
            combined.movies.extend(r.movies)
            combined.showtimes.extend(r.showtimes)
            combined.errors.extend(r.errors)
        return self.save_result(combined, prefix="combined_")