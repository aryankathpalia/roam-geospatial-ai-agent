from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5433/roam"
    GROQ_API_KEY: str = ""

    # Set to "require" for hosted Postgres (e.g. Neon). Leave empty for local docker-compose.
    DATABASE_SSLMODE: str = ""

    # Geo API placeholders — wrappers are not implemented in this skeleton phase.
    NOMINATIM_USER_AGENT: str = "ROAM/0.1 (dev@localhost)"
    NOMINATIM_BASE_URL: str = "https://nominatim.openstreetmap.org"
    OVERPASS_URL: str = "https://overpass-api.de/api/interpreter"
    OSRM_BASE_URL: str = "https://router.project-osrm.org"
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openrouter/free"
    MISTRAL_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    # Not flagship "gemini-3.5-flash": that model's free tier is capped
    # at 20 requests/day (confirmed via a real 429 from this exact
    # account), and separately, dense-image detail-extraction calls to
    # it failed with 503 "high demand" 100% of the time across 3
    # different flagship models tested. The flash-lite variant fixed
    # both -- higher quota and no 503s -- and produced accurate results
    # on a real ParcelMap crop (verified: correct bearings/distances
    # and basis-of-bearings text matching the source document exactly).
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"


settings = Settings()
