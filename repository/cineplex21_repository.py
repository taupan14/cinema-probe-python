"""
repository/cineplex21_repository.py

PERUBAHAN v2:
  - save_cinema_movies() sekarang terima showtimes dan insert ke tabel showtimes
  - Setelah cinema_movie di-upsert, movie_id di-backfill ke Showtime objects
    sebelum insert ke tabel showtimes
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from supabase import Client
from models import Cinema, CinemaMovie, Showtime

logger = logging.getLogger(__name__)

REGION = "ID"
BATCH  = 100


class Cineplex21Repository:
    def __init__(self, supabase: Client):
        self.db = supabase

    # =========================================================================
    # CINEMAS
    # =========================================================================

    def upsert_cinemas(self, cinemas: list[Cinema]) -> dict[str, str]:
        """
        Upsert ke tabel cinemas by (external_id, source).
        Return: {external_id → cinema UUID}
        """
        if not cinemas:
            return {}

        rows = [
            {
                "name":        c.name,
                "chain":       c.chain,
                "city":        c.city,
                "address":     c.address,
                "source":      c.source,
                "external_id": c.external_id,
                "booking_url": c.booking_url.split("?")[0],  # strip ?city_id=N
                "lat":         c.lat,
                "lng":         c.lng,
            }
            for c in cinemas
        ]

        resp = (
            self.db.table("cinemas")
            .upsert(rows, on_conflict="external_id,source")
            .execute()
        )
        logger.info(f"cinemas upserted: {len(resp.data)} rows")

        return {row["external_id"]: row["id"] for row in resp.data}

    # =========================================================================
    # MOVIES
    # =========================================================================

    def _lookup_movie_id(self, title: str, movie_code: str = "") -> Optional[int]:
        """Lookup movies.id by title (case-insensitive)."""
        resp = (
            self.db.table("movies")
            .select("id")
            .ilike("title", title)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]["id"]

        if ":" in title:
            main = title.split(":")[0].strip()
            resp = (
                self.db.table("movies")
                .select("id")
                .ilike("title", f"%{main}%")
                .limit(1)
                .execute()
            )
            if resp.data:
                return resp.data[0]["id"]

        return None

    def _insert_movie(self, detail: dict) -> Optional[int]:
        """Insert film baru. Gunakan tmdb_id negatif sebagai placeholder."""
        title      = detail.get("title") or ""
        movie_code = detail.get("movie_code") or ""
        if not title or not movie_code:
            return None

        existing = self._lookup_movie_id(title, movie_code)
        if existing:
            return existing

        fake_tmdb_id = -(abs(hash(movie_code)) % 999_999_999)
        try:
            resp = (
                self.db.table("movies")
                .insert({
                    "tmdb_id":        fake_tmdb_id,
                    "title":          title,
                    "original_title": title,
                    "overview":       detail.get("overview") or "",
                    "poster_path":    detail.get("poster_path") or "",
                    "trailer_key":    detail.get("trailer_key") or "",
                    "runtime":        detail.get("runtime"),
                    "status":         "Released",
                    "release_date":   date.today().isoformat(),
                })
                .execute()
            )
            if resp.data:
                movie_id = resp.data[0]["id"]
                logger.info(f"Inserted movie '{title}' id={movie_id}")
                return movie_id
        except Exception as e:
            logger.warning(f"Insert movie '{title}': {e}")
            return self._lookup_movie_id(title, movie_code)

        return None

    def _upsert_cast_crew(self, movie_id: int, detail: dict) -> None:
        """Insert cast (movie_cast) dan crew (movie_crew) jika belum ada."""
        # Cast
        cast_raw = detail.get("cast_raw") or ""
        if cast_raw:
            existing = (
                self.db.table("movie_cast")
                .select("id")
                .eq("movie_id", movie_id)
                .limit(1)
                .execute()
            )
            if not existing.data:
                names = [n.strip() for n in cast_raw.split(",") if n.strip()]
                rows = [
                    {
                        "movie_id":    movie_id,
                        "person_id":   -(abs(hash(n)) % 999_999),
                        "name":        n,
                        "order_index": i,
                        "job":         "Actor",
                        "department":  "Acting",
                    }
                    for i, n in enumerate(names[:20])
                ]
                try:
                    self.db.table("movie_cast").insert(rows).execute()
                except Exception as e:
                    logger.warning(f"Insert cast movie_id={movie_id}: {e}")

        # Crew
        crew_rows = []
        for field_key, job, dept in [
            ("director", "Director",   "Directing"),
            ("producer", "Producer",   "Production"),
            ("writer",   "Screenplay", "Writing"),
        ]:
            for name in [n.strip() for n in (detail.get(field_key) or "").split(",") if n.strip()]:
                crew_rows.append({
                    "movie_id":   movie_id,
                    "person_id":  -(abs(hash(f"{name}{job}")) % 999_999),
                    "name":       name,
                    "job":        job,
                    "department": dept,
                })
        if crew_rows:
            existing = (
                self.db.table("movie_crew")
                .select("movie_id")
                .eq("movie_id", movie_id)
                .limit(1)
                .execute()
            )
            if not existing.data:
                try:
                    self.db.table("movie_crew").insert(crew_rows).execute()
                except Exception as e:
                    logger.warning(f"Insert crew movie_id={movie_id}: {e}")

    def _set_category(self, movie_id: int, category: str, sort_order: int = 0) -> None:
        try:
            self.db.table("movie_categories").upsert(
                {
                    "movie_id":   movie_id,
                    "category":   category,
                    "sort_order": sort_order,
                    "region":     REGION,
                },
                on_conflict="movie_id,category,region",
            ).execute()
        except Exception as e:
            logger.warning(f"set_category movie_id={movie_id} {category}: {e}")

    # =========================================================================
    # CINEMA MOVIES + SHOWTIMES
    # =========================================================================

    def save_cinema_movies(
        self,
        cinema_movies: list[CinemaMovie],
        showtimes: list[Showtime],
        cinema_id_map: dict[str, str],
    ) -> None:
        """
        Alur per CinemaMovie:
          1. Resolve movie_id (lookup/insert ke movies)
          2. Insert cast, crew, category
          3. Upsert ke cinema_movies → dapat cinema_movies.id
          4. Backfill movie_id ke Showtime objects yang terkait
          5. Batch insert Showtime ke tabel showtimes
        """
        if not cinema_movies:
            return

        # ── Step 1-3: Resolve movie_id dan upsert cinema_movies ───────────────
        movie_id_by_key: dict[str, Optional[int]] = {}
        # Map cinema_movie.id → db UUID setelah upsert
        cm_db_id_map: dict[str, str] = {}

        cm_rows = []
        for cm in cinema_movies:
            cache_key = cm.movie_code or cm.title
            if cache_key not in movie_id_by_key:
                detail   = getattr(cm, "_raw_detail", {})
                movie_id = self._lookup_movie_id(cm.title, cm.movie_code)
                if not movie_id and detail:
                    movie_id = self._insert_movie(detail)
                movie_id_by_key[cache_key] = movie_id

                if movie_id:
                    self._upsert_cast_crew(movie_id, detail)
                    self._set_category(movie_id, "now_playing")

            movie_id = movie_id_by_key.get(cache_key)
            cm_rows.append({
                "_local_id": cm.id,       # untuk lookup setelah upsert
                "cinema_id":  cm.cinema_id,
                "movie_id":   movie_id,
                "movie_code": cm.movie_code,
                "title":      cm.title,
                "genre":      cm.genre,
                "duration":   cm.duration,
                "age_rating": cm.age_rating,
                "format":     cm.format,
                "show_date":  cm.show_date,
                "source":     cm.source,
            })

        # Upsert cinema_movies (batch)
        for i in range(0, len(cm_rows), BATCH):
            batch = cm_rows[i : i + BATCH]
            db_rows = [{k: v for k, v in row.items() if k != "_local_id"} for row in batch]
            try:
                resp = (
                    self.db.table("cinema_movies")
                    .upsert(
                        db_rows,
                        on_conflict="cinema_id,movie_id,format,show_date",
                    )
                    .execute()
                )
                # Map local_id → db UUID berdasarkan posisi (order preserved)
                for local_row, db_row in zip(batch, resp.data):
                    cm_db_id_map[local_row["_local_id"]] = db_row["id"]
            except Exception as e:
                logger.error(f"cinema_movies upsert batch {i}: {e}")

        logger.info(f"cinema_movies saved: {len(cm_rows)} rows")

        # ── Step 4-5: Insert showtimes ─────────────────────────────────────────
        if not showtimes:
            return

        # Backfill movie_id ke setiap Showtime
        # (movie_id baru diketahui setelah lookup/insert di atas)
        cm_movie_id: dict[str, Optional[int]] = {
            cm.id: movie_id_by_key.get(cm.movie_code or cm.title)
            for cm in cinema_movies
        }
        # Backfill cinema_movie_id (db UUID) dan movie_id
        st_rows = []
        for st in showtimes:
            db_cm_id = cm_db_id_map.get(st.cinema_movie_id)
            if not db_cm_id:
                # cinema_movie gagal upsert, skip showtime ini
                continue
            st_rows.append({
                "cinema_movie_id": db_cm_id,
                "cinema_id":       st.cinema_id,
                "movie_id":        cm_movie_id.get(st.cinema_movie_id),
                "show_date":       st.show_date,
                "show_time":       st.show_time,
                "format":          st.format,
                "source":          st.source,
            })

        total_st = 0
        for i in range(0, len(st_rows), BATCH):
            batch = st_rows[i : i + BATCH]
            try:
                self.db.table("showtimes").upsert(
                    batch,
                    on_conflict="cinema_id,movie_id,format,show_date,show_time",
                ).execute()
                total_st += len(batch)
            except Exception as e:
                logger.error(f"showtimes upsert batch {i}: {e}")

        logger.info(f"showtimes saved: {total_st} rows")

    # =========================================================================
    # UPCOMING
    # =========================================================================

    def save_upcoming(self, upcoming_films: list[dict]) -> None:
        """Insert upcoming movies ke movies + movie_categories."""
        if not upcoming_films:
            return

        saved = 0
        for i, film in enumerate(upcoming_films):
            title      = (film.get("title") or "").strip()
            movie_code = film.get("dc21_parent_movie_code") or ""
            if not title:
                continue

            dur_raw = str(film.get("duration") or "").strip()
            detail  = {
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
            movie_id = self._lookup_movie_id(title, movie_code)
            if not movie_id:
                movie_id = self._insert_movie(detail)
            if movie_id:
                self._upsert_cast_crew(movie_id, detail)
                self._set_category(movie_id, "upcoming", sort_order=i)
                saved += 1

        logger.info(f"upcoming saved: {saved} films")