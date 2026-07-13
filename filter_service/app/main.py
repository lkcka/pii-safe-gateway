"""
FastAPI-приложение Filter Service. Единственный содержательный эндпоинт —
POST /extract-pii: принимает текст (+regex-подсказки), возвращает список
найденных PII-сущностей с координатами в исходном тексте.
Модель загружается один раз при старте процесса, а не при первом запросе.
"""
import logging
import time

from fastapi import FastAPI, HTTPException

from .config import settings
from .llm_engine import get_engine
from .schemas import ExtractPIIRequest, ExtractPIIResponse

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("filter_service.main")

app = FastAPI(
    title="PII Filter Service",
    description="Локальная LLM для семантического поиска персональных данных в тексте.",
    version="0.1.0",
)


@app.on_event("startup")
def _load_model_on_startup() -> None:
    """Грузим модель сразу при старте контейнера, чтобы не было задержки
    на первом реальном запросе от Gateway."""
    logger.info("Инициализация локальной LLM...")
    get_engine()
    logger.info("Filter Service готов к работе.")


@app.get("/health")
def health() -> dict:
    """Проверка готовности: модель загружена, grammar скомпилирована."""
    try:
        get_engine()
        return {"status": "ok", "model_path": settings.filter_model_path}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Модель не готова: {exc}") from exc


@app.post("/extract-pii", response_model=ExtractPIIResponse)
def extract_pii(request: ExtractPIIRequest) -> ExtractPIIResponse:
    """Находит PII в тексте чанка с учётом семантического контекста."""
    if not request.text.strip():
        return ExtractPIIResponse(entities=[])

    engine = get_engine()
    start_time = time.perf_counter()

    try:
        result = engine.extract(request.text, request.hints)
    except ValueError as exc:
        logger.error("Ошибка генерации/парсинга JSON от LLM: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    elapsed = time.perf_counter() - start_time
    logger.info(
        "Обработан чанк длиной %d симв. за %.2f сек, найдено сущностей: %d",
        len(request.text), elapsed, len(result.entities),
    )
    return result