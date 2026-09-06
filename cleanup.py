"""
cleanup.py — remove __pycache__ directories and stub __init__.py files.

Run from the plugin root (or from anywhere — it uses its own location):
    python cleanup.py

Safe to re-run; fully idempotent.

Rules:
- __pycache__:   every one found under the plugin root is deleted (.venv excluded).
- __init__.py:   deleted only when the file contains nothing beyond an optional
                 coding-declaration comment (i.e. no real code).  The root
                 __init__.py is always preserved because it contains classFactory.
"""

import os
import re
import shutil

PLUGIN_ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {".venv", ".git"}

_MEANINGFUL = re.compile(r"^[^#\s]", re.MULTILINE)


def _is_stub_init(path: str) -> bool:
    """True when __init__.py has no executable content (empty or comment-only)."""
    try:
        text = open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return False
    real_lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return len(real_lines) == 0


removed_cache: list[str] = []
removed_inits: list[str] = []

for dirpath, dirnames, filenames in os.walk(PLUGIN_ROOT, topdown=True):
    # Prune dirs we never want to descend into
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

    # ── __pycache__ ──────────────────────────────────────────────────────────
    if "__pycache__" in dirnames:
        target = os.path.join(dirpath, "__pycache__")
        shutil.rmtree(target)
        removed_cache.append(os.path.relpath(target, PLUGIN_ROOT))
        dirnames.remove("__pycache__")

    # ── __init__.py ──────────────────────────────────────────────────────────
    if "__init__.py" in filenames:
        init_path = os.path.join(dirpath, "__init__.py")
        rel = os.path.relpath(init_path, PLUGIN_ROOT)

        if dirpath == PLUGIN_ROOT:
            # Root __init__.py holds classFactory — never remove.
            print(f"  KEPT   {rel}  (classFactory)")
            continue

        if _is_stub_init(init_path):
            os.remove(init_path)
            removed_inits.append(rel)
        else:
            print(f"  KEPT   {rel}  (has content)")

# ── Summary ──────────────────────────────────────────────────────────────────
print(f"\nRemoved {len(removed_cache)} __pycache__ "
      f"director{'ies' if len(removed_cache) != 1 else 'y'}:")
for p in removed_cache:
    print(f"  {p}")

print(f"\nRemoved {len(removed_inits)} stub __init__.py "
      f"file{'s' if len(removed_inits) != 1 else ''}:")
for p in removed_inits:
    print(f"  {p}")
