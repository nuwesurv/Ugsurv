import subprocess
import sys

def install_package(package):
    python_exe = sys.prefix + r"\python.exe"
    response = subprocess.check_call([
        python_exe,
        "-m",
        "pip",
        "install",
        package
    ])
    return response


def solve_dependency_issues():
    all_installed = []
    # Solve Dependecy issues.
    try:
        import fitz
    except:
        response = install_package('PyMuPDF')
        print([ 'Installed the PyMuPDF dependency:',response])
    try:
        from PIL import Image
    except:
        response = install_package('pillow')
        print([ 'Installed the pillow dependency:',response])
        if response == 0:
            all_installed.append('pillow')
    try:
        import pandas
    except:
        response = install_package('pandas')
        print([ 'Installed the pandas dependency:',response])
        if response == 0:
            all_installed.append('pandas')
    try:
        import geopandas
    except:
        response = install_package('geopandas')
        print([ 'Installed the geopandas dependency:',response])
        if response == 0:
            all_installed.append('geopandas')
    try:
        import shapely
    except:
        response = install_package('shapely')
        print([ 'Installed the shapely dependency:',response])
        if response == 0:
            all_installed.append('shapely')
    try:
        import numpy
    except:
        response = install_package('numpy')
        print([ 'Installed the numpy dependency:',response])
        if response == 0:
            all_installed.append('numpy')
            
    return all_installed
