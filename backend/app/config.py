import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str = f"sqlite:///{ROOT / 'data' / 'bank-demo.sqlite3'}"
    mode: str = "demo"
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: str = ""
    model_timeout: float = 8.0
    model_attempts: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            mode=os.getenv("MODEL_MODE", "demo"),
            model_base_url=os.getenv("MODEL_BASE_URL", ""),
            model_name=os.getenv("MODEL_NAME", ""),
            model_api_key=os.getenv("MODEL_API_KEY", ""),
            model_timeout=min(15.0, max(1.0, float(os.getenv("MODEL_TIMEOUT", "8")))),
            model_attempts=min(2, max(1, int(os.getenv("MODEL_ATTEMPTS", "2")))),
        )
