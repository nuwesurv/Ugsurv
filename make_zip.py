"""
Build a clean QGIS plugin ZIP suitable for uploading to plugins.qgis.org.

Usage:
    python make_zip.py

Output: ugsurv.zip in the parent directory of this file.
"""

import os
import zipfile
import pathlib

ROOT = pathlib.Path(__file__).parent
OUT = ROOT.parent / "ugsurv.zip"

EXCLUDE_DIRS = {
    ".venv", "venv", "env",
    "__pycache__",
    ".git",
    ".idea", ".vscode",
    "test",
    "help",
    "dist", "build",
    "qgisflagged",
}

EXCLUDE_EXTS = {
    ".pyc", ".pyo", ".pyd",
    ".dll", ".so", ".dylib",
    ".exe",
    ".zip", ".tar", ".gz",
    ".bak",
}

EXCLUDE_FILES = {
    "make_zip.py",
    "pb_tool.cfg",
    "pylintrc",
    "Makefile",
    "cleanup.py",
    "package_installer.py",
    ".gitignore",
    "CLAUDE.md",
    "README.html",
}


def should_include(rel: pathlib.Path) -> bool:
    parts = rel.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return False
    if rel.suffix.lower() in EXCLUDE_EXTS:
        return False
    if rel.name in EXCLUDE_FILES:
        return False
    return True


with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    for fpath in ROOT.rglob("*"):
        if fpath.is_dir():
            continue
        rel = fpath.relative_to(ROOT.parent)
        if should_include(rel):
            zf.write(fpath, rel)
            print(f"  + {rel}")

print(f"\nCreated: {OUT}")
