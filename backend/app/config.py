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
    model_budget_path: Path | None = None
    catalog_path: Path | None = None
    knowledge_path: Path | None = None
    public_knowledge_path: Path | None = None
    public_activation_path: Path | None = None
    bank_faq_path: Path | None = None
    bank_faq_activation_path: Path | None = None

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
            model_budget_path=Path(os.getenv("MODEL_BUDGET_PATH", str(ROOT / "data/model-budget.sqlite3"))),
            catalog_path=Path(os.environ["DEMO_CATALOG_PATH"]) if os.getenv("DEMO_CATALOG_PATH") else None,
            knowledge_path=Path(os.environ["DEMO_KNOWLEDGE_PATH"]) if os.getenv("DEMO_KNOWLEDGE_PATH") else None,
            public_knowledge_path=(Path(os.getenv("PUBLIC_KNOWLEDGE_PATH", str(
                ROOT / "docs/知识库/交通银行香港/knowledge-base.json")))
                if os.getenv("PUBLIC_KNOWLEDGE_ENABLED", "1") == "1" else None),
            bank_faq_path=(Path(os.getenv("BANK_FAQ_PATH", str(ROOT / "docs/知识库/香港银行客服/knowledge-base.json")))
                           if os.getenv("PUBLIC_KNOWLEDGE_ENABLED", "1") == "1"
                           and os.getenv("BANK_FAQ_ENABLED", "1") == "1" else None),
            bank_faq_activation_path=(Path(os.environ["BANK_FAQ_ACTIVATION_PATH"])
                                      if os.getenv("BANK_FAQ_ACTIVATION_PATH") else None),
            public_activation_path=(Path(os.environ["PUBLIC_ACTIVATION_PATH"])
                                    if os.getenv("PUBLIC_ACTIVATION_PATH") else None),
        )
