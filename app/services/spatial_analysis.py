"""Deterministic spatial analysis operations for ROAM."""

from shapely.geometry import Point, shape

from app.schemas.geospatial_feature import GeospatialFeature
from pyproj import Transformer
from shapely.ops import transform
class SpatialAnalysisError(Exception):
    """Raised when a spatial analysis operation cannot be completed."""


def count_features_in_area(
    boundary: GeospatialFeature,
    features: list[GeospatialFeature],
) -> int:
    """Count point features that fall inside a boundary polygon."""

    if not boundary.geometry:
        raise ValueError("Boundary geometry cannot be empty.")

    try:
        polygon = shape(boundary.geometry)
    except Exception as exc:
        raise SpatialAnalysisError(
            "Could not construct geometry from boundary."
        ) from exc

    if polygon.is_empty or not polygon.is_valid:
        raise SpatialAnalysisError(
            "Boundary geometry must be a valid polygon."
        )

    count = 0

    for feature in features:
        if not feature.geometry:
            continue

        if feature.geometry.get("type") != "Point":
            continue

        coordinates = feature.geometry.get("coordinates")

        if not coordinates or len(coordinates) < 2:
            continue

        try:
            longitude = float(coordinates[0])
            latitude = float(coordinates[1])
        except (TypeError, ValueError):
            continue

        point = Point(longitude, latitude)

        if polygon.contains(point) or polygon.touches(point):
            count += 1

    return count

def calculate_area_km2(
    boundary: GeospatialFeature,
) -> float:
    """Calculate the area of a boundary in square kilometres."""

    if not boundary.geometry:
        raise ValueError("Boundary geometry cannot be empty.")

    try:
        polygon = shape(boundary.geometry)
    except Exception as exc:
        raise SpatialAnalysisError(
            "Could not construct geometry from boundary."
        ) from exc

    if polygon.is_empty or not polygon.is_valid:
        raise SpatialAnalysisError(
            "Boundary geometry must be a valid polygon."
        )

    # EPSG:32643 (UTM Zone 43N) uses metres, allowing us to calculate area
    # correctly. This assumes the boundary falls within that UTM zone
    # (e.g. northern India); a general-purpose implementation should pick
    # the UTM zone from the boundary's centroid instead.
    transformer = Transformer.from_crs(
        "EPSG:4326",
        "EPSG:32643",
        always_xy=True,
    )

    projected_polygon = transform(
        transformer.transform,
        polygon,
    )

    return projected_polygon.area / 1_000_000


def calculate_density(
    feature_count: int,
    area_km2: float,
) -> float:
    """Calculate feature density per square kilometre."""

    if feature_count < 0:
        raise ValueError("Feature count cannot be negative.")

    if area_km2 <= 0:
        raise ValueError("Area must be greater than zero.")

    return feature_count / area_km2