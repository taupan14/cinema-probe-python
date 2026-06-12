"""
models/schemas.py
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Optional
from typing import Any
import uuid


@dataclass
class Cinema:
    name: str
    chain: str
    city: str
    source: str
    external_id: str = ""
    address: str = ""
    lat: float = 0.0
    lng: float = 0.0
    google_maps_url: str = ""
    booking_url: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    def to_db_dict(self) -> dict:
        d = self.to_dict()
        d.pop("id", None)
        d.pop("created_at", None)
        return d


@dataclass
class CinemaMovie:
    cinema_id: str
    title: str
    source: str
    movie_id: Optional[int] = None
    movie_code: str = ""
    genre: str = ""
    duration: str = ""
    age_rating: str = ""
    format: str = "2D"
    show_date: str = field(default_factory=lambda: date.today().isoformat())
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    _raw_detail: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False
    )

    def to_dict(self) -> dict:
        return asdict(self)

    def to_db_dict(self) -> dict:
        d = self.to_dict()
        d.pop("id", None)
        d.pop("created_at", None)
        return d


@dataclass
class Showtime:
    """
    Satu baris = satu jam tayang untuk satu film di satu cinema.

    Field schedule dari dc21-api:
      schedule[].time_show    → show_time
      schedule[].studio_id[]  → studio_id (per jam)
      schedule[].ticket_price → ticket_price
    """
    cinema_movie_id: str        # FK ke cinema_movies.id
    cinema_id: str              # FK ke cinemas.id
    show_date: str              # "YYYY-MM-DD"
    show_time: str              # "HH:MM"
    format: str = "2D"
    source: str = ""
    movie_id: Optional[int] = None
    studio_id: Optional[int] = None      # nomor studio/hall, dari schedule[].studio_id[i]
    ticket_price: Optional[int] = None   # harga dalam rupiah, dari schedule[].ticket_price
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_db_dict(self) -> dict:
        return {
            "cinema_movie_id": self.cinema_movie_id,
            "cinema_id":       self.cinema_id,
            "movie_id":        self.movie_id,
            "show_date":       self.show_date,
            "show_time":       self.show_time,
            "format":          self.format,
            "studio_id":       self.studio_id,
            "ticket_price":    self.ticket_price,
            "source":          self.source,
        }


@dataclass
class ScrapeResult:
    cinemas: list[Cinema] = field(default_factory=list)
    movies: list[CinemaMovie] = field(default_factory=list)
    showtimes: list[Showtime] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def cinema_count(self) -> int:
        return len(self.cinemas)

    @property
    def movie_count(self) -> int:
        return len(self.movies)

    @property
    def showtime_count(self) -> int:
        return len(self.showtimes)