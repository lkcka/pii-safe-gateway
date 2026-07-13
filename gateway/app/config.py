"""
Конфигурация Gateway. Все параметры читаются из переменных окружения
(см. .env.example), значения по умолчанию — безопасные для dev-режима.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Filter Service ---
    filter_service_url: str = "http://filter-service:8001"
    filter_request_timeout: int = 60  # сек

    # --- Внешний OpenAI-совместимый провайдер ---
    external_llm_base_url: str
    external_llm_api_key: str

    # --- Обработка файлов ---
    max_file_size_mb: int = 20
    chunk_max_chars: int = 3000
    chunk_overlap_chars: int = 250

    # --- Прочее ---
    log_level: str = "INFO"


settings = Settings()