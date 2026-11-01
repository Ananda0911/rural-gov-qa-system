"""全局配置：pydantic-settings + .env（密钥与阈值集中一处，改动重启生效）"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 大模型 API（OpenAI 兼容）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_chat_model: str = ""
    llm_embed_model: str = ""
    llm_mock: bool = True  # 默认演示模式，接入真实 API 后置 0

    # 阈值（T1 相关性 / T2 未答聚合 / T3 标题相似提示）
    t1_relevance: float = 0.75
    t2_aggregate: float = 0.82
    t3_similar: float = 0.85

    # 认证安全
    bcrypt_rounds: int = 12
    login_max_fails: int = 5
    lock_minutes: int = 15
    session_days: int = 7

    # 路径
    db_path: str = str(BASE_DIR / "data" / "app.db")
    backup_dir: str = str(BASE_DIR / "data" / "backup")
    upload_dir: str = str(BASE_DIR / "data" / "uploads")  # 文档导入原始文件归档目录
    backup_keep: int = 30  # 备份滚动保留份数

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000
    run_scheduler: bool = True


settings = Settings()
