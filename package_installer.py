import subprocess
import sys


def install_package(package: str) -> int:
    python_exe = sys.prefix + r"\python.exe"
    response = subprocess.check_call([
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