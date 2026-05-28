# 🎬 Indonesia Cinema Scraper

Scraper data bioskop Indonesia (XXI, CGV, Cinepolis) dari **21cineplex.com**, **cgv.id**, dan **cinepolis.co.id** — semua kota di Indonesia.

Data disimpan sesuai skema database Supabase (tabel `cinemas` + `cinema_movies`).

---

## 🏗️ Arsitektur

```
cinema-scraper/
├── main.py                  # Entry point, CLI, orchestrator
├── scrapers/
│   ├── base.py              # Abstract BaseScraper
│   ├── cineplex21.py        # Scraper 21cineplex (XXI + sumber lain)
│   ├── cgv.py               # Scraper CGV Indonesia
│   └── cinepolis.py         # Scraper Cinepolis Indonesia
├── models/
│   └── schemas.py           # Dataclass: Cinema, CinemaMovie, ScrapeResult
├── db/
│   └── storage.py           # SupabaseStorage + LocalStorage (JSON/CSV)
├── utils/
│   ├── http_client.py       # Async HTTP client (retry, rate limit, header rotation)
│   └── helpers.py           # Text cleaning, normalisasi, file I/O
├── output/                  # Hasil scraping (JSON + CSV)
├── logs/                    # Log file per-run
├── requirements.txt
└── .env.example
```

### Tech Stack
| Komponen | Library | Alasan |
|---|---|---|
| HTTP | `httpx` (async) | HTTP/2 support, lebih modern dari requests |
| Parsing | `beautifulsoup4` + `lxml` | Robust HTML parsing |
| Retry | `tenacity` | Exponential backoff yang fleksibel |
| CLI | `argparse` | Standar Python, no dependency |
| UI | `rich` | Progress bar + tabel cantik di terminal |
| Database | Supabase REST API | Direct API, tanpa SDK tambahan |
| Concurrency | `asyncio.gather` | Semua scraper berjalan paralel |

---

## ⚡ Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Setup environment

```bash
cp .env.example .env
# Edit .env sesuai kebutuhan
```

### 3. Jalankan

```bash
# Scrape semua chain
python main.py

# Scrape chain tertentu
python main.py --source 21cineplex
python main.py --source cgv
python main.py --source cinepolis

# Langsung upload ke Supabase
python main.py --save-db

# Test tanpa menyimpan
python main.py --dry-run

# Custom delay (lebih lambat, lebih aman dari rate limit)
python main.py --delay 2.5

# Debug mode
python main.py --log-level DEBUG
```

---

## 🔧 Konfigurasi `.env`

```env
# Supabase (wajib jika --save-db)
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...

# Output
OUTPUT_DIR=./output
SAVE_TO_JSON=true
SAVE_TO_CSV=true

# HTTP tuning
REQUEST_DELAY=1.5      # detik antar request
REQUEST_TIMEOUT=30     # timeout per request
MAX_RETRIES=3          # max retry
```

---

## 📦 Output

Setiap run menghasilkan file di `./output/`:

```
output/
├── 21cineplex_20240509_143000.json        # Raw JSON lengkap
├── 21cineplex_20240509_143000_cinemas.csv # CSV bioskop
├── 21cineplex_20240509_143000_movies.csv  # CSV film
├── cgv_20240509_143000.json
├── cinepolis_20240509_143000.json
└── combined_all_20240509_143000.json      # Semua gabungan
```

### Struktur JSON output
```json
{
  "source": "21cineplex",
  "scraped_at": "2024-05-09T14:30:00",
  "cinema_count": 150,
  "movie_count": 1200,
  "errors": [],
  "cinemas": [
    {
      "id": "uuid-local",
      "name": "XXI Grand Indonesia",
      "chain": "XXI",
      "city": "Jakarta",
      "address": "Jl. MH Thamrin No.1",
      "lat": -6.1944,
      "lng": 106.8229,
      "source": "21cineplex",
      "external_id": "GDI"
    }
  ],
  "movies": [
    {
      "cinema_id": "uuid-local",
      "title": "Godzilla x Kong",
      "genre": "Action, Sci-Fi",
      "duration": "115 min",
      "age_rating": "13+",
      "format": "IMAX 2D",
      "show_date": "2024-05-09"
    }
  ]
}
```

---

## 🗄️ Upload ke Supabase

Scraper menggunakan **upsert** (bukan insert) sehingga aman dijalankan berulang:
- Cinemas: match by `(source, external_id)` — tidak duplikat
- Movies: insert fresh per-run (jadwal berubah setiap hari)

```bash
# Jadwalkan via cron (setiap hari jam 06:00)
0 6 * * * cd /path/to/cinema-scraper && python main.py --save-db >> logs/cron.log 2>&1
```

---

## 🔍 Strategi Scraping

Setiap scraper menggunakan pendekatan berlapis:

1. **API JSON** — dicoba pertama, paling efisien dan akurat
2. **HTML scraping** — fallback jika API tidak tersedia/berubah
3. **Embedded JSON** — extract data dari `<script>` tags (Next.js/Nuxt apps)

### Anti-rate-limit
- Rotating User-Agent headers
- Configurable delay antar request
- Exponential backoff on retry
- Per-instance rate limiter

---

## ➕ Menambah Scraper Baru

1. Buat file `scrapers/namachain.py`
2. Extend `BaseScraper`
3. Override `scrape_cinemas()` dan `scrape_movies()`
4. Register di `scrapers/__init__.py` dan `main.py`

```python
from scrapers.base import BaseScraper

class NamaChainScraper(BaseScraper):
    SOURCE = "namachain"
    CHAIN = "NamaChain"
    BASE_URL = "https://www.namachain.com"

    async def scrape_cinemas(self, client):
        # implementasi
        ...

    async def scrape_movies(self, client, cinema):
        # implementasi
        ...
```

---

## ⚠️ Catatan

- Website bioskop dapat berubah struktur HTML/API kapan saja → scraper mungkin perlu update
- Gunakan delay yang wajar (≥1.5s) untuk menghindari IP block
- Untuk produksi, pertimbangkan menggunakan proxy rotation
- Data koordinat (lat/lng) mungkin tidak tersedia dari semua sumber
