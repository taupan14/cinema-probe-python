"""
main.py
Entry point utama scraper bioskop Indonesia.

Usage:
    python main.py                          # Scrape semua chain
    python main.py --source 21cineplex      # Scrape hanya XXI
    python main.py --source cgv             # Scrape hanya CGV
    python main.py --source cinepolis       # Scrape hanya Cinepolis
    python main.py --save-db               # Langsung simpan ke Supabase
    python main.py --dry-run               # Test tanpa menyimpan
"""
from __future__ import annotations
import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich import print as rprint

load_dotenv()
console = Console()

# ── Logging setup ──────────────────────────────────────────────────────────────
def setup_logging(level: str = "INFO"):
    log_dir = Path("./logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"scrape_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    # Suppress noisy third-party loggers
    for noisy in ("httpx", "httpcore", "hpack", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_file


# ── CLI Arguments ──────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="🎬 Indonesia Cinema Scraper — XXI, CGV, Cinepolis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        choices=["21cineplex", "cgv", "cinepolis", "all"],
        default="all",
        help="Chain yang di-scrape (default: all)",
    )
    parser.add_argument("--save-db", action="store_true", help="Simpan ke Supabase")
    parser.add_argument("--dry-run", action="store_true", help="Jalankan tanpa menyimpan")
    parser.add_argument("--delay", type=float, default=float(os.getenv("REQUEST_DELAY", 1.5)),
                        help="Delay antar request (detik)")
    parser.add_argument("--timeout", type=float, default=float(os.getenv("REQUEST_TIMEOUT", 30)),
                        help="Timeout per request (detik)")
    parser.add_argument("--retries", type=int, default=int(os.getenv("MAX_RETRIES", 3)),
                        help="Max retry per request")
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", "./output"),
                        help="Direktori output")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


# ── Scraper factory ────────────────────────────────────────────────────────────
def get_scrapers(source: str, delay: float, timeout: float, retries: int):
    from scrapers import Cineplex21Scraper, CGVScraper, CinepolisScraper

    kwargs = dict(delay=delay, timeout=timeout, max_retries=retries)
    all_scrapers = {
        "21cineplex": Cineplex21Scraper(**kwargs),
        "cgv": CGVScraper(**kwargs),
        "cinepolis": CinepolisScraper(**kwargs),
    }
    if source == "all":
        return list(all_scrapers.values())
    return [all_scrapers[source]]


# ── Display helpers ────────────────────────────────────────────────────────────
def print_banner():
    console.print(Panel.fit(
        "[bold cyan]🎬 Indonesia Cinema Scraper[/bold cyan]\n"
        "[dim]21cineplex · CGV · Cinepolis — semua kota Indonesia[/dim]",
        border_style="cyan",
    ))


def print_summary_table(results: list):
    table = Table(title="📊 Hasil Scraping", show_header=True, header_style="bold magenta")
    table.add_column("Source", style="cyan", no_wrap=True)
    table.add_column("Cinemas", justify="right", style="green")
    table.add_column("Movies", justify="right", style="yellow")
    table.add_column("Errors", justify="right", style="red")
    table.add_column("Status")

    total_cinemas = total_movies = total_errors = 0
    for r in results:
        status = "✅" if not r.errors else ("⚠️" if r.cinemas else "❌")
        table.add_row(
            r.source,
            str(r.cinema_count),
            str(r.movie_count),
            str(len(r.errors)),
            status,
        )
        total_cinemas += r.cinema_count
        total_movies += r.movie_count
        total_errors += len(r.errors)

    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{total_cinemas}[/bold]",
        f"[bold]{total_movies}[/bold]",
        f"[bold]{total_errors}[/bold]",
        "📊",
    )
    console.print(table)


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    args = parse_args()
    log_file = setup_logging(args.log_level)
    print_banner()

    console.print(f"[dim]Log: {log_file}[/dim]")
    console.print(f"[dim]Source: {args.source} | Delay: {args.delay}s | Save DB: {args.save_db}[/dim]\n")

    scrapers = get_scrapers(args.source, args.delay, args.timeout, args.retries)
    results = []

    # ── Run scrapers ─────────────────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        tasks_map = {}
        for scraper in scrapers:
            t = progress.add_task(
                f"[cyan]Scraping {scraper.SOURCE} ({scraper.CHAIN})...[/cyan]",
                total=None,
            )
            tasks_map[scraper.SOURCE] = t

        # Jalankan semua scraper secara concurrent
        scraper_tasks = [scraper.run() for scraper in scrapers]
        completed = await asyncio.gather(*scraper_tasks, return_exceptions=True)

        for i, (scraper, result) in enumerate(zip(scrapers, completed)):
            task_id = tasks_map[scraper.SOURCE]
            if isinstance(result, Exception):
                progress.update(task_id, description=f"[red]❌ {scraper.SOURCE}: {result}[/red]")
                from models import ScrapeResult
                results.append(ScrapeResult(source=scraper.SOURCE, errors=[str(result)]))
            else:
                from models import ScrapeResult  # pastikan ScrapeResult diimport
                assert isinstance(result, ScrapeResult)  # bantu type checker narrowing
                progress.update(
                    task_id,
                    description=f"[green]✅ {scraper.SOURCE}: {result.cinema_count} cinemas, {result.movie_count} movies[/green]",
                )
                results.append(result)

    console.print()
    print_summary_table(results)

    if args.dry_run:
        console.print("\n[yellow]⚠️  Dry run — tidak ada data yang disimpan.[/yellow]")
        return

    # ── Save output ──────────────────────────────────────────────────────────
    from db import LocalStorage, SupabaseStorage

    local = LocalStorage(args.output_dir)

    # Simpan per-source
    saved_paths = []
    for result in results:
        if result.cinemas or result.movies:
            paths = local.save_result(result)
            saved_paths.extend(paths.values())

    # Simpan combined
    if len(results) > 1:
        combined_paths = local.save_combined(results)
        saved_paths.extend(combined_paths.values())

    console.print(f"\n[green]💾 File tersimpan di:[/green] [bold]{args.output_dir}[/bold]")
    for p in saved_paths[:6]:
        console.print(f"   [dim]→ {p}[/dim]")

    # ── Supabase ─────────────────────────────────────────────────────────────
    if args.save_db:
        supabase_url = os.getenv("SUPABASE_URL", "")
        supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
        if not supabase_url or not supabase_key:
            console.print("[red]❌ SUPABASE_URL dan SUPABASE_SERVICE_KEY harus diset di .env[/red]")
        else:
            console.print("\n[cyan]🔄 Uploading ke Supabase...[/cyan]")
            db = SupabaseStorage(supabase_url, supabase_key)
            total_cin = total_mov = total_st = 0

            for result in results:
                # 1. Upsert cinemas
                cin_count = await db.upsert_cinemas(result.cinemas)
                total_cin += cin_count

                # 2. Ambil mapping: external_id → supabase UUID
                cinema_id_map = await db.get_cinema_ids(result.source)

                # 3. Buat local UUID → supabase UUID
                local_to_db = {}
                for cinema in result.cinemas:
                    supabase_uuid = cinema_id_map.get(cinema.external_id)
                    if supabase_uuid:
                        local_to_db[cinema.id] = supabase_uuid

                # 4. Upsert cinema_movies
                # Side-effect: db._last_movie_id_cache & db._cm_key_to_db_uuid
                # diisi otomatis untuk dipakai upsert_showtimes()
                mov_count = await db.upsert_movies(result.movies, local_to_db)
                total_mov += mov_count

                # 5. Upsert showtimes (CGV & scraper lain yang support)
                if result.showtimes:
                    st_count = await db.upsert_showtimes(result.showtimes, local_to_db)
                    total_st += st_count

            console.print(
                f"[green]✅ Supabase: {total_cin} cinemas + {total_mov} movies "
                f"+ {total_st} showtimes uploaded[/green]"
            )

    console.print("\n[bold green]🎬 Scraping selesai![/bold green]")


if __name__ == "__main__":
    asyncio.run(main())