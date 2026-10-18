# Backend API

Локальный FastAPI Backend принимает `.mov`, проверяет контейнер через FFprobe, регистрирует задание в SQLite, обрабатывает bounded-очередь одним in-process worker и извлекает реальные JPEG-кадры через FFmpeg.

Режим `APP_ANALYZER_MODE=local` подключает существующий offline DINOv2 + сохранённую Logistic-голову.
Настройка, запуск с Logistic v4 и независимая проверка — в [local-ml.md](../docs/local-ml.md).
Локальный Swagger, безопасная CPU Docker-сборка через публичный TAR и синтетические
проверки описаны в [container-local.md](../docs/container-local.md).
`FakeAnalyzer` доступен только для development/test. Production-конфигурация с ним отклоняется;
при отключённом анализаторе задания переходят в `failed`. Ошибка загрузки local-модели останавливает startup.

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
uvicorn fortebank_api.main:app --app-dir src --host 127.0.0.1 --port 8001 --no-proxy-headers
```

Для запуска API текущего этапа оставьте `APP_ENVIRONMENT=development` и `APP_ANALYZER_MODE=fake`. Это режим проверки API, а не AI-анализ.

## Переменные окружения

| Переменная | Назначение | Значение примера |
|---|---|---|
| `APP_API_KEY` | Секрет для `X-API-Key` | заменить локально |
| `APP_UI_ALLOWED_ORIGINS` | JSON-массив точных Origin без завершающего `/`; production только HTTPS | `["http://localhost:5173"]` |
| `APP_UI_SESSION_TTL_SEC` | абсолютный срок сессии, 60–3600 секунд, без продления polling | `1800` |
| `APP_ANALYZER_MODE` | `disabled`, dev/test `fake`, offline `local` | `fake` в примере; default `disabled` |
| `APP_ML_PRIVATE_ROOT` | граница локальных модельных артефактов | `private_data` |
| `APP_ML_CHECKPOINT` | обученная Logistic-голова; путь относительно cwd | `private_data/checkpoints/logistic-v4/head.pt` |
| `APP_ML_MODEL_DIR` | проверенный offline snapshot DINOv2 | `private_data/weights/dinov2-small` |
| `APP_ML_DEVICE` | `auto`, `cpu`, `mps`; голова остаётся на CPU | `auto` |
| `APP_ML_BATCH_SIZE` | backbone batch size | `4` |
| `APP_STORAGE_DIR` | непубличное хранилище видео | `./storage` |
| `APP_DATABASE_PATH` | SQLite состояния заданий | `./storage/jobs.sqlite3` |
| `APP_MAX_UPLOAD_BYTES` | максимальный размер самого файла | 128 MiB |
| `APP_UPLOAD_CHUNK_BYTES` | размер блока потокового копирования | 1 MiB |
| `APP_JURY_UPLOAD_TIMEOUT_SEC` | полный приём HTTP-тела API жюри | `60` |
| `APP_MAX_JURY_UPLOADS` | одновременные запросы загрузки жюри | `4` |
| `APP_JOB_RETENTION_SEC` | срок хранения терминальных JSON/JPEG (командное решение) | `86400` |
| `APP_CLEANUP_INTERVAL_SEC` | период очистки, до 100 старых результатов за проход | `300` |
| `APP_MAX_QUEUE_SIZE` | число ожидающих заданий, без активного | `8` |
| `APP_ANALYSIS_TIMEOUT_SEC` | общий timeout анализатора и evidence, без ожидания в очереди | `240` |
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
curl -X POST http://127.0.0.1:8001/api/v1/analyze \
  -H "X-API-Key: replace-with-your-local-secret" \
  -F "file=@/tmp/fortebank-synthetic.mov;type=video/quicktime"
```

Ответ `202` содержит только `job_id`. Подставьте его в запрос состояния:

```bash
curl http://127.0.0.1:8001/api/v1/jobs/JOB_ID \
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

Частичные и завершившие обработку видео удаляются. Ошибка evidence никогда не преобразуется в `no_violation`. Внутренние причины и метрики времени не попадают в публичный JSON. При старте и штатной остановке незавершённые задания финализируются как `failed`, а связанные файлы удаляются. При остановке/падении worker запрос незавершённого задания возвращает `failed`, новые задания и `/health` — `503`; требуется restart процесса. `/health=200` не доказывает подключение ML.

`APP_STORAGE_DIR` — отдельный приватный каталог только этого экземпляра приложения, не папка датасета. При старте после финализации незавершённых заданий удаляются оставшиеся файлы собственного формата `32-lowercase-hex.mov`, в том числе потерянные до регистрации в SQLite. Симлинки, подпапки и посторонние имена не обходятся. Нельзя запускать два процесса с одним storage/SQLite.

API жюри проверяет ключ **до чтения multipart**. HTTP-тело ограничено `APP_MAX_UPLOAD_BYTES + 64 KiB`, в том числе при chunked/неверном Content-Length; отдельно проверяется размер файла. Переполнение даёт `413`, медленная загрузка — `408`, исчерпание одновременных загрузок — `503`. FFprobe/FFmpeg разрешены только MOV demuxer и локальные `file,pipe`, сетевые протоколы не разрешены. Отмена FFprobe завершает дочерний процесс.

## Ограничения worker

Реализован один in-process worker. Backend следует запускать одним Uvicorn-процессом без `--workers N`. Это не распределённая очередь: незавершённые задания после перезапуска не возобновляются, а безопасно финализируются как `failed`.

## Web UI bridge без ручного входа

Внутренние маршруты: `POST /ui-api/session` (пустое тело, автоматическое создание/восстановление анонимной сессии), `POST /ui-api/analyze` (multipart `file`) и `GET /ui-api/jobs/{job_id}`. Ручной login и logout удалены; GET/DELETE `/ui-api/session` возвращают `405`. `APP_UI_ACCESS_CODE` не используется. Они исключены из OpenAPI жюри. Оба транспорта вызывают общий `analysis_service.py`, прежние SQLite и worker, без HTTP-запросов Backend к самому себе. Публичный JSON и авторизация жюри не изменены.

Сессия возвращает только CSRF-token, фактический лимит файла и признак `fake` / `disabled` / `local`. Это отдельный внутренний протокол UI. Ключ жюри не используется bridge и не нужен Vite.

### Запуск в Fish, терминал 1

Из корня проекта после установки зависимостей:

```fish
cd backend
source .venv/bin/activate.fish
read --silent --prompt-str 'API key жюри: ' --export APP_API_KEY
set -gx APP_ENVIRONMENT development
set -gx APP_ANALYZER_MODE fake
set -gx APP_UI_ALLOWED_ORIGINS '["http://localhost:5173"]'
uvicorn fortebank_api.main:app --app-dir src --host 127.0.0.1 --port 8001 --no-proxy-headers
```

Используйте случайный секрет API жюри, например из менеджера паролей; UI-кода больше нет. `read --silent` не отображает значение и не записывает его в shell history. Альтернатива — закрытый локальный `backend/.env` по корневому `.env.example`, не попадающий в Git. Существующий `APP_UI_ACCESS_CODE` можно удалить из локальной конфигурации: он игнорируется. Терминал 2 описан в [Frontend README](../frontend/README.md).

### Защита и ограничения

- Случайный 256-битный токен; сервер хранит его SHA-256 и сессию в памяти. Cookie: `HttpOnly; SameSite=Strict; Path=/ui-api`, без Domain; production добавляет `Secure`.
- Все изменяющие UI-запросы требуют разрешённого Origin, включая bootstrap. Upload дополнительно требует `X-CSRF-Token`. Bootstrap не принимает credentials, не загружает видео и не меняет действующую сессию/TTL; он возвращает CSRF для дальнейших запросов. Cross-site Fetch Metadata отклоняется, CORS не включён. Проверки выполняются до чтения тела.
- Глобально 10 bootstrap-запросов в минуту (включая успешные и восстановление существующей сессии), максимум 64 сессии. Лимитер не зависит от подделываемых forwarded headers. Недостаток простого общего лимита: злоумышленник может временно исчерпать его для остальных; при `429` повторить через минуту.
- Глобально 6 попыток upload в минуту, 2 одновременных HTTP-upload, 1 незавершённое задание на сессию, 64 задания за срок сессии. Ошибочные авторизованные uploads тоже расходуют лимит. Прежний лимит очереди сохраняется.
- Multipart-body: максимум `APP_MAX_UPLOAD_BYTES + 64 KiB` на служебные данные; файл — строго `APP_MAX_UPLOAD_BYTES`. Проверяется поток, не только Content-Length; приём тела — 60 секунд. Bootstrap принимает только пустое тело; чтение ограничено 4 KiB, старый login JSON отклоняется. Частичные multipart-файлы закрываются при ошибках парсинга/лимита.
- UI видит только задания своей сессии; её набор ID — метаданные доступа, не вторая очередь. Expiry/restart/очистка cookies закрывают UI-доступ к прежним результатам, но не отменяют worker. Новая анонимная сессия не наследует чужие или истёкшие задания. Refresh восстанавливает сессию, но не выбранный файл/историю. API жюри остаётся доступен только по ключу.
- Bridge отвечает с `Cache-Control: no-store`, `nosniff` и безопасными ошибками. Не логировать cookies, API key, CSRF, тела запросов и кадры в приложении или reverse proxy.

### Production — пока не развёрнуто

Нужны HTTPS и один доверенный origin для статического `frontend/dist` и `/ui-api`, проксируемого в FastAPI без изменения Origin/cookies. Не кэшировать bridge; ограничить тела/соединения/таймауты на reverse proxy; не включать wildcard CORS. Backend напрямую в сеть не выставлять, использовать один worker. Vite dev server не является production-сервером.

Настройки: `APP_ENVIRONMENT=production`, `APP_DEBUG=false`, секрет API жюри, непустой список HTTPS в `APP_UI_ALLOWED_ORIGINS`. UI общедоступен; анонимная сессия изолирует задания, но не идентифицирует людей и не заменяет инфраструктурную защиту от злоупотреблений. FakeAnalyzer запрещён и конфигурацией, и проверкой внедрённого объекта. Пока модели нет, `disabled` даёт `failed`, а не ложный отрицательный вердикт. Реальный локальный адаптер — отдельный TASK 05B.

С TASK 06A терминальные JSON/JPEG очищаются по `APP_JOB_RETENTION_SEC` (24 часа по умолчанию): до 100 записей при старте и каждые `APP_CLEANUP_INTERVAL_SEC`. Активные и недавние результаты не удаляются; TTL отсчитывается от завершения, очистка не мгновенная. Истёкший ID даёт `404`; TTL сессии отдельно и не удаляет БД. Согласуйте retention с организаторами до банковских данных.

Удалённые SQLite-страницы переиспользуются, но файл на диске автоматически не уменьшается. `secure_delete` не гарантирует стирание SSD/WAL/снимков/резервных копий; нужны приватные права, шифрование диска и согласованное удаление данных после хакатона. Не запускайте VACUUM/удаление БД под работающим worker. Контролируйте свободное место; длительный поток результатов может опередить очистку.

Конкретные systemd/NGINX/TLS-настройки, ограничения и непроверенные этапы: [deployment.md](../docs/deployment.md). Измерения: [readiness-report.md](../docs/readiness-report.md).

### Проверки интеграции

```fish
python -m pytest
python -m compileall -q src tests
python -m pip check
# При свободных 8001 и 5173:
env PYTHONPATH=src python -m tests.http_e2e
# Если 5173 занят, тестовый порт и разрешённый Origin можно сменить вместе:
env PYTHONPATH=src FORTE_HTTP_UI_PORT=5174 FORTE_HTTP_BACKEND_PORT=8002 python -m tests.http_e2e
# Изолированный HTTP benchmark: 3 повтора для 10/60/145-секундной синтетики.
env PYTHONPATH=src python -m tests.benchmark
```

HTTP-тест требует установленных Frontend dependencies, Node, FFmpeg и разрешённых loopback-сокетов. Сам создаёт временные секреты/MOV, запускает Vite + FastAPI, проверяет анонимный bootstrap/cookie/CSRF и изоляцию двух посетителей, SQLite/worker, `processing → done`, несколько событий/JPEG, `failed`, повтор и неизменную защиту API жюри, затем останавливает свои процессы. Это **HTTP E2E, не автоматизация браузера**. Реальный ML, пяти­минутный SLA на целевом железе и публичный deployment не проверены.
