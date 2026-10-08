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
    # Sign-in: the Google OAuth web client id (Google Cloud console -> Credentials), the secret that signs
    # ROAM's own session tokens (generated and kept in data/.session_secret when empty), and the Google
    # accounts that may change the shared sample documents (comma-separated).
    GOOGLE_CLIENT_ID: str = ""
    SESSION_SECRET: str = ""
    ADMIN_EMAILS: str = ""
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "openrouter/free"
    MISTRAL_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    # .env holds two rotation keys instead of a plain GEMINI_API_KEY; a
    # plain `uvicorn app.main:app` launch must still find one, or every
    # Gemini stage (sheet triage, parcel roster, extraction) silently fails.
    GEMINI_API_KEY_1: str = ""
    GEMINI_API_KEY_2: str = ""

    def model_post_init(self, __context) -> None:
        if not self.GEMINI_API_KEY:
            self.GEMINI_API_KEY = self.GEMINI_API_KEY_1 or self.GEMINI_API_KEY_2
    # Not flagship "gemini-3.5-flash": that model's free tier is capped
    # at 20 requests/day (confirmed via a real 429 from this exact
    # account), and separately, dense-image detail-extraction calls to
    # it failed with 503 "high demand" 100% of the time across 3
    # different flagship models tested. The flash-lite variant fixed
    # both -- higher quota and no 503s -- and produced accurate results
    # on a real ParcelMap crop (verified: correct bearings/distances
    # and basis-of-bearings text matching the source document exactly).
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"
    # Tried in order when the primary returns 503 "high demand" --
    # Google's per-model server capacity, not our quota, so a different
    # model often succeeds. Comma-separated; empty disables fallback.
    # Kept to the flash-lite family: flagship gemini-3.5-flash is a
    # poor fallback (20 req/day free-tier cap, see above), and
    # gemini-2.5-flash-lite returns 404 for new users.
    GEMINI_FALLBACK_MODELS: str = "gemini-3.1-flash-lite"
    # confirm-boundary calibration: one extra Gemini call per confirm
    # (single polygon overlay) to read per-edge printed values OCR
    # proximity misses. Candidates still pass calibration's normal checks.
    CALIBRATION_GEMINI_ASSOCIATION: bool = True


settings = Settings()
