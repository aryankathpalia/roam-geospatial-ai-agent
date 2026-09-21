from app.services.spatial_analysis import calculate_density


hospital_count = 1
area_km2 = 1.138

density = calculate_density(
    feature_count=hospital_count,
    area_km2=area_km2,
)

print(f"Hospital density: {density:.3f} hospitals/km²")