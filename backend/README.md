# Backend API

Локальный FastAPI Backend принимает `.mov`, проверяет контейнер через FFprobe, регистрирует задание в SQLite, обрабатывает bounded-очередь одним in-process worker и извлекает реальные JPEG-кадры через FFmpeg.

Настоящий ML inference ещё не интегрирован. `FakeAnalyzer` доступен только для development/test и возвращает тестовое `no_violation`. Production-конфигурация с `FakeAnalyzer` отклоняется; при отключённом анализаторе задания переходят в `failed`.

## Системные требования

- Python 3.11+;
- `ffprobe` для проверки файлов и временной шкалы;
- `ffmpeg` для локального извлечения evidence frames и генерации синтетических тестов.

Проверка:

```bash
ffprobe -version
ffmpeg -version
```

FFmpeg/FFprobe вызываются локально без shell-конкатенации и без сетевых запросов. Лицензию конкретной сборки FFmpeg необходимо учитывать отдельно при её распространении.

## Установка и запуск

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp ../.env.example .env
```

Замените `APP_API_KEY` в `.env` на локально сгенерированное случайное значение. Не используйте примерный ключ в production.

```bash
uvicorn fortebank_api.main:app --app-dir src --host 127.0.0.1 --port 8000
```

Для запуска API текущего этапа оставьте `APP_ENVIRONMENT=development` и `APP_ANALYZER_MODE=fake`. Это режим проверки API, а не AI-анализ.

## Переменные окружения

| Переменная | Назначение | Значение примера |
|---|---|---|
| `APP_API_KEY` | Секрет для `X-API-Key` | заменить локально |
| `APP_ANALYZER_MODE` | `disabled` или dev/test `fake` | `fake` |
| `APP_STORAGE_DIR` | непубличное хранилище видео | `./storage` |
| `APP_DATABASE_PATH` | SQLite состояния заданий | `./storage/jobs.sqlite3` |
| `APP_MAX_UPLOAD_BYTES` | максимальный размер самого файла | 128 MiB |
| `APP_UPLOAD_CHUNK_BYTES` | размер блока потокового копирования | 1 MiB |
| `APP_MAX_QUEUE_SIZE` | число ожидающих заданий, без активного | `8` |
| `APP_ANALYSIS_TIMEOUT_SEC` | timeout анализатора | `240` |
| `APP_FFPROBE_PATH` | локальный executable FFprobe | `ffprobe` |
| `APP_FFPROBE_TIMEOUT_SEC` | timeout проверки контейнера | `10` |
| `APP_FFMPEG_PATH` | локальный executable FFmpeg | `ffmpeg` |
| `APP_EVIDENCE_TIMEOUT_SEC` | timeout одного FFmpeg/FFprobe-вызова evidence | `10` |
| `APP_EVIDENCE_MAX_WIDTH` | максимальная ширина JPEG без upscaling | `1280` |
| `APP_EVIDENCE_MAX_HEIGHT` | максимальная высота JPEG без upscaling | `720` |
| `APP_EVIDENCE_MAX_JPEG_BYTES` | максимальный размер одного JPEG | `4 MiB` |
| `APP_EVIDENCE_JPEG_QUALITY` | FFmpeg MJPEG quality, 2–31 (меньше — лучше) | `3` |
| `APP_EVIDENCE_ENDPOINT_SCAN_SEC` | ограниченное окно поиска последнего кадра | `2` |

Лимит 128 MiB оставляет запас относительно типичных 29 МБ из FAQ и не превращает параметры обучающего набора в жёсткую границу. Полный пяти­минутный SLA с настоящим ML пока не подтверждён.

## Проверка API синтетическим видео

Создайте однотонный MOV без реальных данных:

```bash
ffmpeg -v error -f lavfi -i color=c=black:s=32x32:r=2 -t 1 -an -c:v mjpeg -f mov -y /tmp/fortebank-synthetic.mov
```

Загрузите его, используя значение ключа из локального `.env`:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/analyze \
  -H "X-API-Key: replace-with-your-local-secret" \
  -F "file=@/tmp/fortebank-synthetic.mov;type=video/quicktime"
```

Ответ `202` содержит только `job_id`. Подставьте его в запрос состояния:

```bash
curl http://127.0.0.1:8000/api/v1/jobs/JOB_ID \
  -H "X-API-Key: replace-with-your-local-secret"
```

Возможны только `processing`, `done` и `failed`. В fake-режиме корректный синтетический файл завершается `no_violation`; это технический результат заглушки, а не вывод AI.

## Тесты

```bash
python -m pytest
```

Тесты используют временные SQLite/хранилища, mocks и только синтетические медиа. Цветные и VFR-сценарии проверяют соответствие кадра временному участку, реальные JPEG/base64, один и несколько событий, до трёх кадров, ошибки FFmpeg и очистку MOV. Тесты не требуют банковских видео, GPU или ML-весов.

## Evidence frames и точность времени

`VideoEvidenceExtractor` принимает 1–3 конечных неотрицательных таймкода в пределах ролика. FFmpeg делает accurate seek, автоматически применяет rotation metadata, ограничивает размер кадра и возвращает JPEG через pipe. Временные JPEG-файлы не создаются.

В `timestamp_sec` записывается не запрошенное значение, а фактический PTS декодированного кадра из фильтра FFmpeg `showinfo`, нормализованный к началу видеопотока. При запросе после последнего PTS, включая точную длительность контейнера, FFprobe ищет последний доступный кадр только в ограниченном конечном окне. Поэтому кадровая точность зависит от временной шкалы, частоты кадров и корректности PTS исходного MOV; абсолютная frame-accurate гарантия для произвольного повреждённого/VFR-файла не заявляется.

## Поведение при ошибках и перезапуске

- неверный/отсутствующий ключ: `401`;
- API-ключ не настроен: `503`;
- отсутствующее поле `file`: `422`;
- расширение не `.mov`: `415`;
- пустой, повреждённый или неподтверждённый FFprobe файл: `400`;
- превышение размера: `413`;
- FFprobe недоступен или очередь заполнена: `503`;
- неизвестный `job_id`: `404`;
- ошибка/timeout анализатора или обязательного evidence extraction: публичный статус `failed`.

Частичные и завершившие обработку видео удаляются. Ошибка evidence никогда не преобразуется в `no_violation`. Внутренние причины и метрики времени не попадают в публичный JSON. При старте и штатной остановке незавершённые задания финализируются как `failed`, а связанные файлы удаляются.

## Ограничения worker

Реализован один in-process worker. Backend следует запускать одним Uvicorn-процессом без `--workers N`. Это не распределённая очередь: незавершённые задания после перезапуска не возобновляются, а безопасно финализируются как `failed`.
