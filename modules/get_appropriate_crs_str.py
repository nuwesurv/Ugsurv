from qgis.core import QgsProject, QgsCoordinateTransform, QgsCoordinateReferenceSystem

def get_canvas_epsg(canvas):
    """
    Returns the appropriate UTM EPSG code for the current map canvas extent.
    Assumes Northern or Southern Hemisphere based on center latitude.
    """
    # 1. Get map canvas
    extent = canvas.extent()  # QgsRectangle in current CRS
    src_crs = canvas.mapSettings().destinationCrs()

    # 2. Transform center point to WGS84 (lat/lon)
    center_x = (extent.xMinimum() + extent.xMaximum()) / 2
    center_y = (extent.yMinimum() + extent.yMaximum()) / 2

    dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")  # WGS84
    xform = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
    lon, lat = xform.transform(center_x, center_y)

    # 3. Compute UTM zone
    zone = int((lon + 180) / 6) + 1

    # 4. Determine hemisphere
    if lat >= 0:
        epsg = f"326{zone}"  # Northern Hemisphere
    else:
        epsg = f"327{zone}"  # Southern Hemisphere

    return epsg