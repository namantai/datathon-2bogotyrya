# DATATHON — ForteBank

Локальный сервис анализа видеозаписей кассовых операций для выявления визуальных признаков возможных нарушений. Проект разрабатывается для ForteBank DATATHON 2026.

## Team

Two developers: ML and Fullstack.

## Branches

- `main` — stable integration branch
- `feature/ml` — machine learning
- `feature/fullstack` — backend and frontend

## Project status

TASK 06A: локально работают React → защищённый same-origin `/ui-api` → FastAPI → bounded worker/SQLite → FFmpeg JPEG. Dashboard открывается без пароля и регистрации. Автоматическая анонимная HttpOnly-сессия сохраняет CSRF/Origin-защиту и изоляцию заданий. API жюри сохраняет `X-API-Key` и исходный JSON. Добавлены ранние ограничения загрузки, обработка остановленного worker и настраиваемая очистка старых результатов.

**Настоящая модель пока не подключена.** `FakeAnalyzer` разрешён только в dev/test и явно помечен в UI; production его отклоняет. `disabled` даёт `failed`, не выдуманный вердикт. Нормативная карта и ML-контракт требуют согласования. Публичный HTTPS ещё не развёрнут. Измерения синтетики не являются оценкой качества AI или доказательством SLA.

## Стек

- Backend: Python 3.11+, FastAPI, Pydantic, pytest.
- Frontend: React, TypeScript, Vite.
- ML: будущий локальный Python inference-модуль; контракт находится в статусе DRAFT.

## Структура

```text
backend/   FastAPI-приложение и Backend-тесты
frontend/  React-приложение
ml/        область ответственности ML Engineer
configs/   совместные конфигурации правил и категорий
docs/      архитектура, требования и контракты
scripts/   будущие воспроизводимые служебные сценарии
```

## Docker: один локальный адрес

Подготовьте приватный `backend/.env` с `APP_API_KEY` (существующий файл не перезаписывать).
Из корня проекта на Arch Linux: `sudo docker compose up --build -d --wait` →
`http://localhost:8080`. Backend наружу не опубликован, SQLite хранится в named volume.
Это **локальный HTTP development-стенд**, анализатор по умолчанию `disabled`.
Для проверки на синтетике: `sudo docker compose -f compose.yaml -f compose.local-fake.yaml up --build -d --wait`.
FakeAnalyzer не является AI-моделью и запрещён в production. Docker runtime-проверки
пока требуют локального запуска с доступом к daemon: TASK D02 — PARTIAL.
Инструкции Fish, smoke/restart, секреты, лицензии и MacBook:
[docs/docker-deployment.md](docs/docker-deployment.md).

## Локальный запуск Backend (Fish, терминал 1)

```fish
cd backend
python3 -m venv .venv
source .venv/bin/activate.fish
python -m pip install -e '.[dev]'
read --silent --prompt-str 'API key жюри: ' --export APP_API_KEY
set -gx APP_ENVIRONMENT development
set -gx APP_ANALYZER_MODE fake
set -gx APP_UI_ALLOWED_ORIGINS '["http://localhost:5173"]'
uvicorn fortebank_api.main:app --app-dir src --host 127.0.0.1 --port 8001 --workers 1 --no-proxy-headers --no-access-log
```

Нужны установленные локально FFmpeg/FFprobe. API key жюри — случайный секрет ≥16 символов из менеджера паролей. Для Web UI пароль не требуется; устаревший `APP_UI_ACCESS_CODE` больше не используется. Не вводите секреты прямо в команду/history. Альтернатива — приватный `backend/.env` по [.env.example](.env.example), без коммита. `GET http://127.0.0.1:8001/health` проверяет процесс/worker, **не готовность ML**. Настройки: [backend/README.md](backend/README.md).

## Локальный запуск Frontend (Fish, отдельный терминал без секретов)

```fish
cd frontend
npm ci
set -gx VITE_UI_MODE bridge
set -gx UI_PROXY_TARGET http://127.0.0.1:8001
npm run dev
```

Откройте `http://localhost:5173`: Dashboard виден сразу, а сессия создаётся автоматически. API key в браузер не вводится. Используйте только синтетику до согласования обработки данных. Production build: `npm run build`; Vite dev/preview не является production HTTPS-сервером. Сценарии и тесты: [frontend/README.md](frontend/README.md).

## Проверки и ограничения

Из `backend/`: `python -m pytest`, `python -m compileall -q src tests`, `python -m pip check`.
Из `frontend/`: `npm run typecheck`, `npm run build`, `npm test`, `npm run test:production`, `npm audit --audit-level=high`.
Из корня: `git diff --check`.

HTTP E2E и замеры (Fish, из `backend/`, свободные локальные порты, зависимости установлены):

```fish
env PYTHONPATH=src FORTE_HTTP_UI_PORT=5174 FORTE_HTTP_BACKEND_PORT=8002 python -m tests.http_e2e
env PYTHONPATH=src python -m tests.benchmark
```

Оба сценария генерируют только синтетические MOV в приватных временных каталогах, закрывают собственные процессы и удаляют свои данные. Это HTTP-проверки, не браузерная автоматизация. Benchmark: 10/60/145 с, по 3 запроса, два тестовых события / шесть исходных JPEG на результат.

Один Uvicorn-процесс, один worker; restart завершает незавершённые задания как `failed`, а сессии UI сбрасывает. MOV удаляются после обработки. Терминальные JSON/JPEG хранятся 24 часа по умолчанию (командное решение, настраивается), очищаются пакетами; недавние результаты сохраняются при restart. Истёкшие результаты дают `404`. Это не персональная система аудита и не долговременный архив. Изоляция действует между cookie-сессиями (например, разными профилями браузера), не между людьми за общим браузером.

Замеры выполнены на CPU i5-12450H / ~15.3 GiB RAM, без ML. Это **не минимальные требования для модели**. GPU/VRAM, веса, лицензии и запас RAM должен подтвердить ML Engineer. Для публичного стенда нужен постоянно включённый компьютер, приватное локальное хранилище, доступный HTTPS и отдельно проверенный лимит 5 минут с очередью и реальной моделью.

## Документация

- [Архитектура](docs/architecture.md)
- [Требования](docs/requirements.md)
- [Контракт API жюри](docs/api-contract.md)
- [DRAFT-контракт Backend ↔ ML](docs/ml-contract.md)
- [План HTTPS-развёртывания и передача ML](docs/deployment.md)
- [Локальный Docker Compose: запуск и проверки D02](docs/docker-deployment.md)
- [Аудит надёжности, измерения и ограничения TASK 06A](docs/readiness-report.md)
- [Рабочий процесс Codex](docs/codex-workflow.md)

Общие обязательные правила разработки и безопасности находятся в [AGENTS.md](AGENTS.md).
