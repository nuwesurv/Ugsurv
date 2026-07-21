import re
import subprocess
import sys

_SAFE_PACKAGE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*(\[.*?\])?(==|>=|<=|!=|~=|>|<)[A-Za-z0-9._*-]+$|^[A-Za-z0-9][A-Za-z0-9._-]*$')


def install_package(package: str) -> int:
    if not _SAFE_PACKAGE_RE.match(package):
        raise ValueError(f"Refusing to install unsafe package name: {package!r}")
    python_exe = sys.prefix + r"\python.exe"
    response = subprocess.check_call([  # nosec B603
        python_exe,
        "-m",
        "pip",
        "install",
        package
    ])
    return response


def solve_dependency_issues() -> list[str]:
    all_installed = []

    dependencies = [
        ("fitz",     "PyMuPDF"),
        ("PIL",      "pillow"),
        ("pandas",   "pandas"),
        ("geopandas","geopandas"),
        ("shapely",  "shapely"),
        ("numpy",    "numpy"),
    ]

    for module, package in dependencies:
        try:
            __import__(module)
        except ImportError:
            response = install_package(package)
            print([f"Installed the {package} dependency:", response])
            if response == 0:
                all_installed.append(package)

    return all_installed