"""
inspect_json.py
===============
Lihat struktur dalam dari JSON yang berhasil di-fetch.
Jalankan: python inspect_json.py

Tidak perlu internet — baca dari probe_output/ yang sudah ada.
"""

import json
from pathlib import Path


def inspect(path: str, label: str):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"\n{'='*60}")
    print(f"[ {label} ]  {path}")
    print(f"{'='*60}")

    def dig(obj, depth=0, max_depth=6, prefix=""):
        indent = "  " * depth
        if depth > max_depth:
            print(f"{indent}...")
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    print(f"{indent}{prefix}{k}:")
                    dig(v, depth + 1, max_depth)
                else:
                    val_str = str(v)[:100]
                    print(f"{indent}{prefix}{k}: {val_str!r}")
        elif isinstance(obj, list):
            print(f"{indent}[list: {len(obj)} items]")
            for i, item in enumerate(obj[:3]):
                print(f"{indent}  [{i}]:")
                dig(item, depth + 1, max_depth)
            if len(obj) > 3:
                print(f"{indent}  ... (+{len(obj)-3} more)")

    dig(data)


# ── Inspect semua file yang ada ───────────────────────────────────────────────
probe_dir = Path("probe_output")

for f in sorted(probe_dir.glob("*.json")):
    if f.stat().st_size < 10:
        print(f"\n[SKIP] {f.name} — kosong")
        continue
    inspect(str(f), f.name)