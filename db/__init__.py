# db/__init__.py
# Re-export agar main.py bisa import dari kedua cara:
#   from db import LocalStorage, SupabaseStorage
#   from db.storage import LocalStorage, SupabaseStorage
from db.storage import LocalStorage, SupabaseStorage

__all__ = ["LocalStorage", "SupabaseStorage"]