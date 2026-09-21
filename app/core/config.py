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


settings = Settings()
