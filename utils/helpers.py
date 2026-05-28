"""
utils/helpers.py
Helper functions: text cleaning, normalization, dll.
"""
from __future__ import annotations
import re
import json
from pathlib import Path
from datetime import date
from typing import Any, Union


def clean_text(text: str) -> str:
    if not text:
        return ""
    return " ".join(text.split()).strip()


def normalize_city(city: str) -> str:
    """Normalisasi nama kota ke Title Case."""
    city = clean_text(city)
    # Hapus prefix 'Kota ' yang kadang muncul
    city = re.sub(r"^(Kota|Kabupaten)\s+", "", city, flags=re.IGNORECASE)
    return city.title()


def extract_duration(text: str) -> str:
    """Ekstrak durasi dari string, output: '120 min'."""
    if not text:
        return ""
    m = re.search(r"(\d+)\s*(min|menit|jam|hour|hrs?)", text, re.IGNORECASE)
    if m:
        val, unit = m.group(1), m.group(2).lower()
        if unit in ("jam", "hour", "hrs", "hr"):
            return f"{int(val)*60} min"
        return f"{val} min"
    # Coba ekstrak angka saja
    m = re.search(r"(\d{2,3})", text)
    if m:
        return f"{m.group(1)} min"
    return clean_text(text)


def normalize_format(fmt: str) -> str:
    """Normalisasi format tayang: 2D, 3D, IMAX, 4DX, dst."""
    if not fmt:
        return "2D"
    fmt = fmt.upper().strip()
    known = ["IMAX", "4DX", "SCREENX", "4DX SCREEN", "DOLBY", "PLF", "MX4D"]
    for k in known:
        if k in fmt:
            # Combine dengan dimensi jika ada
            dim = "3D" if "3D" in fmt else "2D"
            return f"{k} {dim}"
    if "3D" in fmt:
        return "3D"
    return "2D"


def normalize_age_rating(rating: str) -> str:
    if not rating:
        return ""
    rating = rating.upper().strip()
    # Mapping common ratings
    # Check prefixed ratings first (more specific)
    if "D17" in rating:
        return "D17+"
    if "D21" in rating:
        return "21+"
    mapping = {
        "SU": "SU", "13+": "13+", "13": "13+",
        "17+": "17+", "17": "17+",
        "21+": "21+", "21": "21+", "R": "17+", "PG": "SU",
        "PG-13": "13+", "G": "SU",
    }
    for k, v in mapping.items():
        if k in rating:
            return v
    return rating


def save_json(data: Any, filepath: Union[str, Path]):
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def save_csv(rows: list, filepath: Union[str, Path]):
    import csv
    if not rows:
        return
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def today_iso() -> str:
    return date.today().isoformat()
