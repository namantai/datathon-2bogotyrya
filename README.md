# DATATHON — ForteBank

Локальный сервис анализа видеозаписей кассовых операций для выявления визуальных признаков возможных нарушений. Проект разрабатывается для ForteBank DATATHON 2026.

## Team

Two developers: ML and Fullstack.

## Branches

- `main` — stable integration branch
- `feature/ml` — machine learning
- `feature/fullstack` — backend and frontend

## Project status

Этап 03: защищённый Backend API дополнен локальным извлечением настоящих JPEG evidence frames через FFmpeg с фактическими PTS и ресурсными ограничениями. Настоящий ML inference и полноценный пользовательский интерфейс пока отсутствуют.

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

## Локальный запуск Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp ../.env.example .env
uvicorn fortebank_api.main:app --app-dir src --reload
```

Проверка: `GET http://127.0.0.1:8000/health`.
Подробные настройки и примеры API: [backend/README.md](backend/README.md).

## Локальный запуск Frontend

```bash
cd frontend
npm install
npm run dev
```

Production-сборка проверяется командой `npm run build`.

## Документация

- [Архитектура](docs/architecture.md)
- [Требования](docs/requirements.md)
- [Контракт API жюри](docs/api-contract.md)
- [DRAFT-контракт Backend ↔ ML](docs/ml-contract.md)
- [Рабочий процесс Codex](docs/codex-workflow.md)

Общие обязательные правила разработки и безопасности находятся в [AGENTS.md](AGENTS.md).
