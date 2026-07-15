import logging
from typing import Any, Optional

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import settings
from .schemas import ChatCompletionRequest
from .extractors import extract_text
from .chunker import chunk_text
from .regex_hints import find_hints
from .anonymizer import anonymize_text

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gateway.main")

app = FastAPI(
    title="PII-Safe-Gateway",
    description="OpenAI-compatible reverse-proxy с очисткой файлов от PII через локальную LLM.",
    version="0.1.0",
)

_http_external: Optional[httpx.AsyncClient] = None
_http_filter: Optional[httpx.AsyncClient] = None


def _external_chat_completions_url() -> str:
    base = settings.external_llm_base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _filter_extract_pii_url() -> str:
    return settings.filter_service_url.rstrip("/") + "/extract-pii"


@app.on_event("startup")
async def _startup() -> None:
    global _http_external, _http_filter

    ext_headers = {}
    if settings.external_llm_api_key:
        ext_headers["Authorization"] = f"Bearer {settings.external_llm_api_key}"

    _http_external = httpx.AsyncClient(headers=ext_headers)

    _http_filter = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.filter_request_timeout)
    )

    logger.info("Gateway started.")
    logger.info("Filter service URL: %s", settings.filter_service_url)
    logger.info("External LLM base URL: %s", settings.external_llm_base_url)


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _http_external, _http_filter
    if _http_external is not None:
        await _http_external.aclose()
        _http_external = None
    if _http_filter is not None:
        await _http_filter.aclose()
        _http_filter = None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
    retry=retry_if_exception_type(httpx.RequestError),
)
async def _call_filter_service(text: str, hints: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Вызов Filter Service с retry на сетевые ошибки (не на 4xx).
    """
    if _http_filter is None:
        raise RuntimeError("Filter HTTP client not initialized")

    resp = await _http_filter.post(
        _filter_extract_pii_url(),
        json={"text": text, "hints": hints},
    )
    # 5xx тоже считаем ошибкой (не PII-safe продолжать)
    if resp.status_code >= 500:
        raise httpx.RequestError(f"Filter service 5xx: {resp.status_code}", request=resp.request)

    # 4xx — это наша ошибка запроса, retry не нужен
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Filter service error: {resp.status_code} {resp.text}")

    return resp.json()


def _inject_documents_into_messages(messages: list[dict[str, Any]], docs_block: str) -> list[dict[str, Any]]:
    """
    Чтобы не ломать возможные сложные структуры messages (tools / multimodal),
    добавляем отдельное user-сообщение с документами.
    """
    out = list(messages)
    out.append({"role": "user", "content": docs_block})
    return out


async def _process_files_and_anonymize(files: list[dict[str, Any]]) -> str:
    """
    Возвращает единый текстовый блок документов, уже очищенный от PII.
    """
    docs_parts: list[str] = []

    for f in files:
        filename = f.get("filename")
        content_base64 = f.get("content_base64")
        if not filename or not content_base64:
            raise HTTPException(status_code=400, detail="Each file must have filename and content_base64")

        # Лимит размера (очень грубо по base64; точный после decode внутри extractors)
        approx_bytes = (len(content_base64) * 3) // 4
        if approx_bytes > settings.max_file_size_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File too large: {filename}")

        original_text = extract_text(filename, content_base64)
        if not original_text.strip():
            docs_parts.append(f'=== {filename} ===\n(пустой текстовый слой)\n')
            continue

        chunks = chunk_text(
            original_text,
            max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars,
        )

        global_entities: list[dict[str, Any]] = []
        for ch in chunks:
            hints = find_hints(ch.text)

            data = await _call_filter_service(ch.text, hints)

            for e in data.get("entities", []):
                # координаты в ответе filter-service локальные для чанка
                try:
                    local_start = int(e["start"])
                    local_end = int(e["end"])
                    ent_text = str(e["text"])
                    ent_type = str(e["type"]).strip().upper()
                except Exception:  # noqa: BLE001
                    continue

                global_start = ch.start_idx + local_start
                global_end = ch.start_idx + local_end

                global_entities.append(
                    {
                        "start": global_start,
                        "end": global_end,
                        "text": ent_text,
                        "type": ent_type,
                    }
                )

        anonymized_text, applied = anonymize_text(original_text, global_entities)
        logger.info(
            "File processed: %s | len=%d | chunks=%d | entities=%d | applied=%d",
            filename, len(original_text), len(chunks), len(global_entities), len(applied),
        )

        docs_parts.append(f"=== {filename} ===\n{anonymized_text}\n")

    return "\n".join(docs_parts)


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest = Body(...)) -> Response:
    """
    OpenAI-compatible endpoint.
    Расширение: принимает поле `files` (JSON), очищает текст и добавляет в messages.
    Дальше проксирует в внешний OpenAI-compatible API.
    """
    if _http_external is None:
        raise HTTPException(status_code=503, detail="External HTTP client not initialized")

    payload = body.model_dump(mode="python")  # including extra fields
    files = payload.pop("files", None)
    stream = bool(payload.get("stream", False))

    # Если файлы есть — чистим и добавляем к messages (fail-closed: если не удалось — 4xx/5xx)
    if files:
        try:
            docs_block = await _process_files_and_anonymize(files)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process files")
            raise HTTPException(status_code=502, detail=f"Failed to process files: {exc}") from exc

        payload["messages"] = _inject_documents_into_messages(payload["messages"], docs_block)

    url = _external_chat_completions_url()

    # --- non-stream ---
    if not stream:
        try:
            resp = await _http_external.post(url, json=payload, timeout=300.0)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"External LLM unreachable: {exc}") from exc

        content_type = resp.headers.get("content-type", "application/json")
        return Response(content=resp.content, status_code=resp.status_code, media_type=content_type)

    # --- stream=true passthrough (SSE) ---
    try:
        upstream = _http_external.stream("POST", url, json=payload, timeout=None)
        resp_ctx = await upstream.__aenter__()
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"External LLM unreachable: {exc}") from exc

    async def _iter() -> Any:
        try:
            async for chunk in resp_ctx.aiter_raw():
                yield chunk
        finally:
            await upstream.__aexit__(None, None, None)

    content_type = resp_ctx.headers.get("content-type", "text/event-stream")
    return StreamingResponse(_iter(), status_code=resp_ctx.status_code, media_type=content_type)