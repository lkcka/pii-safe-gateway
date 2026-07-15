# PII-Safe-Gateway

Reverse-proxy (шлюз) для корпоративного доступа к внешним LLM (OpenAI-совместимый API),
который перед отправкой запроса **автоматически очищает вложенные документы от персональных данных (PII)**
с помощью локальной LLM, работающей внутри периметра.

Проект реализован в виде двух сервисов:

- **Gateway (FastAPI, порт 8000)** — OpenAI-compatible `/v1/chat/completions`, расширение API полем `files`.
  Извлекает текст из файлов, режет на чанки, ищет regex-кандидаты (hints), вызывает локальный Filter Service,
  заменяет найденные PII на маркеры `[PERSON]`, `[PHONE]`, `[EMAIL]`, ... и только затем проксирует запрос во внешний LLM.
- **Filter Service (FastAPI + llama.cpp, порт 8001)** — локальная LLM для семантического NER: получает чанк текста и hints,
  решает контекстно “PII это или нет”, возвращает JSON со сущностями и координатами.

---

## Статус проекта

✅ Этап 2.2: Filter Service реализован, есть self-test на старте контейнера.  
✅ Этап 2.3: Gateway реализован end-to-end:
- поддержка TXT/DOCX/PDF (без OCR),
- чанкинг с overlap,
- regex hints (email/phone/date/long numbers),
- вызов Filter Service `/extract-pii`,
- безопасная замена сущностей на маркеры,
- проксирование `/v1/chat/completions` во внешний OpenAI-compatible API,
- прозрачный passthrough `stream: true` (SSE) без модификации контента.

⚠️ Известные ограничения текущей версии:
- Производительность Filter Service на CPU с Qwen2.5-7B Instruct может быть ~15–20 сек на чанк (качество важнее скорости).
- При наличии hints локальная LLM **может** вернуть сущности без `start/end` (в таком случае сущности отбрасываются валидатором,
  что снижает полноту анонимизации). Это будет исправляться следующей итерацией (fallback на координаты hints / усиление промпта).
- DOCX: извлекаются параграфы и таблицы в порядке следования в документе; пустые абзацы сохраняются.

---

## Архитектура

```
┌──────────────┐        ┌─────────────────────────┐       ┌──────────────────┐
│   Клиент     │───────▶│   PII Gateway (FastAPI)  │──────▶│  Внешний LLM API │
│ (OpenAI SDK) │        │  порт :8000              │       │  (OpenAI/др.)     │
└──────────────┘        └───────────┬──────────────┘       └──────────────────┘
                                     │ (если есть files)
                                     ▼
                         ┌─────────────────────────┐
                         │  Filter Service          │
                         │  (llama.cpp + FastAPI)   │
                         │  порт :8001              │
                         └─────────────────────────┘
```

### Почему NER + замена по индексам (а не “переписать текст без PII”)
Потому что переписывание ломает структуру документа: таблицы, нумерации, форматирование, ссылки.
NER-подход позволяет делать **точечные**, проверяемые замены по координатам.

---

## Расширение OpenAI API: поле `files`

Gateway реализует OpenAI-compatible `POST /v1/chat/completions` и расширяет его полем:

```json
"files": [
  {"filename": "contract.docx", "content_base64": "...."},
  {"filename": "scan.pdf", "content_base64": "...."}
]
```

Это расширение удобно тестировать (без multipart), но **не является стандартом OpenAI API**.
Gateway удаляет поле `files` перед отправкой во внешний LLM.

---

## Поддерживаемые типы PII (маркеризация)

Заменяем найденное на маркеры:

- `[PERSON]`
- `[PHONE]`
- `[EMAIL]`
- `[ADDRESS]`
- `[DATE_OF_BIRTH]`
- `[SNILS]`
- `[INN]`
- `[PASSPORT]`
- `[OTHER]`

---

## Требования

- Docker Engine 24+
- Docker Compose v2
- ~15 ГБ свободного места (сборка + веса модели)
- Рекомендуемое железо: 8+ CPU threads, 16+ GB RAM (тестировалось на Ryzen 7 8845H, 32GB)

---

## Быстрый старт

### 0) Клонирование и env

```bash
git clone https://github.com/<ваш-username>/pii-safe-gateway.git
cd pii-safe-gateway
cp .env.example .env
```

### 1) Скачать модель (GGUF)

```bash
mkdir -p models
pip install -U "huggingface_hub[cli]"
huggingface-cli download bartowski/Qwen2.5-7B-Instruct-GGUF \
  Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  --local-dir ./models --local-dir-use-symlinks False

mv models/Qwen2.5-7B-Instruct-Q4_K_M.gguf models/qwen2.5-7b-instruct-q4_k_m.gguf
```

### 2) Настроить внешний LLM (пример OpenRouter)

В `.env` укажите:

```env
EXTERNAL_LLM_BASE_URL=https://openrouter.ai/api/v1
EXTERNAL_LLM_API_KEY=sk-or-v1-xxxxxxxx
```

Примечание: если оставить дефолт `https://api.openai.com/v1`, то нужен именно OpenAI ключ.

### 3) Запуск (CPU)

```bash
docker compose --profile cpu up -d --build
```

### 4) Проверка здоровья

- Gateway: http://localhost:8000/health
- Filter Service: http://localhost:8001/health

Swagger UI:
- Gateway: http://localhost:8000/docs
- Filter Service: http://localhost:8001/docs

---

## Testing Gateway with files

Smoke-тесты с реальными файлами (TXT/DOCX/PDF) без pytest и без дополнительных
зависимостей. Требуется запущенный Filter Service и настроенный
`EXTERNAL_LLM_BASE_URL` в `.env`.

### Запуск стека

```powershell
docker compose --profile cpu up -d --build
```

```powershell
curl http://localhost:8000/health
curl http://localhost:8001/health
```

Swagger UI Gateway: **http://localhost:8000/docs** — поле `files` видно в схеме
`POST /v1/chat/completions`.

### Генерация sample.docx с PII (таблица + абзацы)

```powershell
docker compose exec gateway python scripts/create_sample_docx.py
```

Скрипт создаёт `gateway/scripts/sample.docx` (ФИО в абзаце, телефон/email в
таблице), сохраняет `sample.docx.b64.txt` и печатает base64 в stdout.

### DOCX smoke test (PowerShell)

**Готовый скрипт:**

```powershell
cd gateway\scripts
.\smoke_test_gateway.ps1 -GatewayUrl "http://localhost:8000" -Model "openai/gpt-4o-mini"
```

**Вручную:**

```powershell
$b64 = (Get-Content -Path "gateway\scripts\sample.docx.b64.txt" -Raw).Trim()

$body = @{
  model = "openai/gpt-4o-mini"
  stream = $false
  messages = @(@{ role="user"; content="Выведи дословно содержимое приложенного документа, включая таблицу." })
  files = @(@{ filename="sample.docx"; content_base64=$b64 })
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/v1/chat/completions" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

Ожидаемые маркеры в ответе (если LLM повторяет документ): `[PERSON]`, `[PHONE]`,
`[EMAIL]`.

### Проверка логов (без PII)

```powershell
docker compose logs --tail=50 gateway
```

Ожидаемая строка (без исходных ФИО/телефона/email):

```
File processed: sample.docx | len=... | chunks=... | entities=... | applied>=2
```

Gateway логирует только `filename`, `len`, `chunks`, `entities`, `applied`.

### TXT smoke test

```powershell
@'
Заявку подал Иванов Иван Иванович, тел. +7 921 555-12-34, email ivanov@example.com.
'@ | Set-Content -Encoding UTF8 sample.txt

$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("sample.txt"))
```

Для `.txt` декодирование: `utf-8-sig` → `utf-8` → `cp1251` (`errors="replace"`).
Base64 декодируется строго (`validate=True`).

### PDF smoke test

1. Создайте PDF с текстовым слоем (Word → «Сохранить как PDF», или «Печать →
   Microsoft Print to PDF») с PII, например:
   `Иванов Иван, tel +7 921 555-12-34, ivanov@example.com`
2. Закодируйте:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\sample.pdf")) | Set-Content sample.pdf.b64.txt
```

3. Отправьте аналогично DOCX, заменив `filename` на `sample.pdf`.

Зашифрованные PDF не поддерживаются — Gateway вернёт `Encrypted PDF not supported`.

---

## Тестирование через PowerShell (Windows)

### 1) Создать файл и получить base64

```powershell
@'
Заявку подал Иванов Иван Иванович, тел. +7 921 555-12-34, email ivanov@example.com.
'@ | Set-Content -Encoding UTF8 sample.txt

$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("sample.txt"))
```

### 2) Non-stream запрос через Gateway

```powershell
$body = @{
  model = "openai/gpt-4o-mini"
  messages = @(@{ role="user"; content="Повтори документ дословно (он уже должен быть анонимизирован маркерами)." })
  files = @(@{ filename="sample.txt"; content_base64=$b64 })
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post `
  -Uri "http://localhost:8000/v1/chat/completions" `
  -ContentType "application/json" `
  -Body $body
```

### 3) Stream запрос (SSE passthrough) через curl.exe

```powershell
$body = @{
  model = "openai/gpt-4o-mini"
  stream = $true
  messages = @(@{ role="user"; content="Напиши 5 строк." })
  files = @(@{ filename="sample.txt"; content_base64=$b64 })
} | ConvertTo-Json -Depth 10

$body | curl.exe -N "http://localhost:8000/v1/chat/completions" `
  -H "Content-Type: application/json" `
  --data-binary "@-"
```

---

## Конфигурация (.env)

Ключевые параметры:

- `EXTERNAL_LLM_BASE_URL`, `EXTERNAL_LLM_API_KEY` — внешний OpenAI-compatible провайдер
- `FILTER_SERVICE_URL` — адрес filter-service внутри docker-сети (обычно `http://filter-service:8001`)
- `CHUNK_MAX_CHARS`, `CHUNK_OVERLAP_CHARS` — чанкинг документов для локальной модели
- `FILTER_MODEL_PATH`, `FILTER_MODEL_THREADS`, `FILTER_MODEL_CTX` — параметры локальной LLM

---

## Структура репозитория

```
pii-safe-gateway/
├── docker-compose.yml
├── .env.example
├── models/
├── gateway/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── scripts/              # create_sample_docx.py, smoke_test_gateway.ps1
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── schemas.py
│       ├── extractors.py
│       ├── chunker.py
│       ├── regex_hints.py
│       └── anonymizer.py
└── filter_service/
    ├── Dockerfile
    ├── Dockerfile.gpu
    ├── requirements.txt
    └── app/
        ├── main.py
        ├── config.py
        ├── prompts.py
        ├── schemas.py
        ├── llm_engine.py
        └── grammar.py
```

---

## Политика безопасности

- Gateway очищает только входящие документы.
- Reverse-mapping (восстановление PII в ответах модели) в текущей версии не реализован.
- OCR для сканированных PDF не реализован (используется только текстовый слой).

---

## Roadmap

- Исправление устойчивости Filter Service при hints (fallback на координаты hints при пропуске start/end)
- Оптимизация скорости (GPU профиль Vulkan / более лёгкая модель / сокращение reason / протокол “вердикты по hints id”)
- Более строгая наблюдаемость: coverage hints, метрики