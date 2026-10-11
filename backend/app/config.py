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
    v15_enabled: bool = False
    v15_budget_path: Path | None = None
    asr_enabled: bool = False
    asr_model_path: Path = ROOT / "data/models/ggml-small.bin"
    asr_cli_path: Path = Path("/opt/homebrew/bin/whisper-cli")
    asr_ffmpeg_path: Path = Path("/opt/homebrew/bin/ffmpeg")
    asr_enhanced: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            asr_enabled=os.getenv("LOCAL_ASR_ENABLED", "0") == "1",
            asr_enhanced=os.getenv("LOCAL_ASR_ENHANCED", "1") == "1",
            asr_model_path=Path(os.getenv("LOCAL_ASR_MODEL", str(cls.asr_model_path))),
            asr_cli_path=Path(os.getenv("LOCAL_ASR_CLI", str(cls.asr_cli_path))),
            asr_ffmpeg_path=Path(os.getenv("LOCAL_ASR_FFMPEG", str(cls.asr_ffmpeg_path))),
            v15_enabled=os.getenv("V15_ENABLED", "0") == "1",
            v15_budget_path=Path(os.getenv("V15_BUDGET_PATH", str(ROOT / "data/model-budget-v15.sqlite3"))),
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
                ROOT / "docs/知识库/东亚银行香港/public-base.json")))
                if os.getenv("PUBLIC_KNOWLEDGE_ENABLED", "1") == "1" else None),
            bank_faq_path=(Path(os.getenv("BANK_FAQ_PATH", str(ROOT / "docs/知识库/东亚银行香港/knowledge-base.json")))
                           if os.getenv("PUBLIC_KNOWLEDGE_ENABLED", "1") == "1"
                           and os.getenv("BANK_FAQ_ENABLED", "1") == "1" else None),
            bank_faq_activation_path=(Path(os.environ["BANK_FAQ_ACTIVATION_PATH"])
                                      if os.getenv("BANK_FAQ_ACTIVATION_PATH") else ROOT / "docs/知识库/东亚银行香港/activation.json"),
            public_activation_path=Path(os.getenv("PUBLIC_ACTIVATION_PATH", str(ROOT / "docs/知识库/东亚银行香港/public-activation.json"))),
        )
