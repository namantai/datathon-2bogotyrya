# План локального HTTPS-стенда — TASK 06A

Статус: **план, не выполненное публичное развёртывание**. Проверены локальные HTTP-сценарии, production bundle и настройки приложения. NGINX/Caddy на машине проверки не установлены; домен, сертификат, NAT и внешняя сеть не проверены. Ниже конфигурация для проверки администратором, не утверждение о работающем публичном URL. Изменения ОС/маршрутизатора и передача данных не выполнялись.

## 1. Топология и условия допуска

Один компьютер команды: NGINX:443 раздаёт `frontend/dist` и проксирует `/ui-api/*`, `/api/v1/*` на `127.0.0.1:8001`. Один Uvicorn-процесс, один worker, приватные SQLite/видео/веса на этом компьютере. Никаких облачных AI, CDN для видео, внешнего туннеля или дополнительной БД.

До открытия порта нужны:

- согласованный с организаторами способ доступа и retention данных;
- контролируемый домен, публичный IP, доступ к настройкам firewall/NAT; при CGNAT сначала решить доступ с провайдером/организаторами, не включать туннель молча;
- доверенный TLS-сертификат для домена и процедура продления; ключ сертификата остаётся локально;
- постоянно включённый компьютер, отключённый сон на время проверки, стабильная сеть и питание;
- работающий локальный ML-адаптер, лицензированные веса и подтверждённый пяти­минутный end-to-end бюджет. Текущий `disabled` **не готов к оценке обнаружения**.

Публичные адреса после проверки: `https://<domain>/` и `https://<domain>/api/v1/`. API key передаётся жюри отдельно по защищённому каналу. UI открывается без пароля, задания изолируются анонимной HttpOnly-cookie. Порты 8001/5173 наружу не публикуются. Открывается только 443; 80 — лишь если нужен выбранной процедуре сертификата/перенаправления, без приёма видео по HTTP.

## 2. Машина и размещение

Локальные замеры: i5-12450H, 12 логических CPU, ~15.3 GiB RAM, Python 3.14, Node 22, FFmpeg/FFprobe. Это стенд Fullstack без ML, **не гарантированный минимум GPU/RAM для inference**. Python проекта ≥3.11; версии на целевой машине проверить тестами.

Предварительно выделить хотя бы 10 GiB свободного диска **без весов** и контролировать свободное место. Это эксплуатационный запас, не измеренный максимум. При default-лимитах до 6 HTTP uploads (4 jury + 2 UI) могут занимать и multipart spool, и сохранённую копию; очередь — до 8 MOV, плюс активный. Результаты с JPEG и WAL также занимают место. Пиковые RAM/VRAM с моделью ещё измерить.

Администратор подготавливает отдельного непривилегированного пользователя `forteguard` и каталоги:

| Путь | Назначение / права |
|---|---|
| `/srv/forteguard/backend` | код/venv; сервис читает, не меняет |
| `/srv/forteguard/frontend/dist` | только production static, доступен NGINX |
| `/srv/forteguard/weights` | проверенные локальные веса, только чтение сервису |
| `/var/lib/forteguard/uploads` | только upload-файлы приложения, владелец forteguard, `0700` |
| `/var/lib/forteguard/tmp` | приватный multipart spool, владелец forteguard, `0700` |
| `/var/lib/forteguard/jobs.sqlite3` | БД вне static root, доступ только сервису |
| `/etc/forteguard/service.env` | секреты/настройки, root, `0600`, не в Git |

Не использовать каталог датасета как uploads: startup удаляет оставшиеся файлы собственного UUID-формата. Не копировать `.env`, SQLite, видео, веса или логи в `dist`. Диск с данными шифровать; резервные копии с JPEG требуют отдельного согласования/срока удаления.

## 3. Конфигурация и запуск

Установка и dev-команды Fish — в [README](../README.md). Production build выполняется в отдельном окружении **без APP_* секретов**: `npm ci`, `npm run typecheck`, `npm run build`, `npm run test:production`. Раздавать только полученный `dist`, не Vite dev/preview.

В приватном `service.env` администратор задаёт следующие значения (API key генерируется локально, примерного значения для него здесь нет):

```dotenv
APP_ENVIRONMENT=production
APP_DEBUG=false
APP_ANALYZER_MODE=disabled
APP_UI_ALLOWED_ORIGINS='["https://jury.example.invalid"]'
APP_UI_SESSION_TTL_SEC=1800
APP_STORAGE_DIR=/var/lib/forteguard/uploads
APP_DATABASE_PATH=/var/lib/forteguard/jobs.sqlite3
APP_MAX_UPLOAD_BYTES=134217728
APP_MAX_JURY_UPLOADS=4
APP_JURY_UPLOAD_TIMEOUT_SEC=60
APP_MAX_QUEUE_SIZE=8
APP_ANALYSIS_TIMEOUT_SEC=240
APP_JOB_RETENTION_SEC=86400
APP_CLEANUP_INTERVAL_SEC=300
APP_FFPROBE_PATH=/usr/bin/ffprobe
APP_FFMPEG_PATH=/usr/bin/ffmpeg
```

Дополнительно обязателен `APP_API_KEY`. `APP_UI_ACCESS_CODE` удалён из настроек: входа Web UI больше нет. Заменить `jury.example.invalid` на фактический HTTPS-домен без `/` в конце. `APP_ANALYZER_MODE=disabled` безопасно отклоняет анализ. Теперь реализован `APP_ANALYZER_MODE=local` с отдельными `APP_ML_*` настройками: см. [локальную ML-интеграцию](local-ml.md). Приведённый выше deployment-план намеренно оставляет `disabled`; не включайте обработку банковских материалов в облаке без подтверждённого разрешения. Пяти­минутный бюджет настоящего ML ещё нужно измерить.

Шаблон `/etc/systemd/system/forteguard.service` (после создания пользователя, каталогов и установки зависимостей):

```ini
[Unit]
Description=ForteGuard local backend
After=network.target

[Service]
Type=simple
User=forteguard
Group=forteguard
WorkingDirectory=/srv/forteguard/backend
EnvironmentFile=/etc/forteguard/service.env
Environment=TMPDIR=/var/lib/forteguard/tmp
UMask=0077
ExecStart=/srv/forteguard/backend/.venv/bin/uvicorn fortebank_api.main:app --app-dir src --host 127.0.0.1 --port 8001 --workers 1 --no-proxy-headers --no-access-log --limit-concurrency 16 --timeout-graceful-shutdown 20
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/forteguard

[Install]
WantedBy=multi-user.target
```

Опции сверены с установленным `uvicorn --help`; unit здесь не запускался. После интеграции factory обновить `ExecStart` согласованно. Проверки перед включением: `systemd-analyze verify /etc/systemd/system/forteguard.service`, права каталогов, запуск FFmpeg/модели от пользователя сервиса. Не использовать `--reload`, несколько workers или одновременный dev-сервер на той же БД. `Restart=on-failure` перезапускает процесс, но не лечит умершую asyncio-task внутри живого процесса: при `/health=503` оператор перезапускает сервис и выясняет причину.

## 4. HTTPS и reverse proxy

Сначала администратор получает сертификат на свой домен, проверяет DNS и резервирует 443. Пример server-блока NGINX внутри `http {}`; заменить домен/пути сертификатов. Не помещать проект целиком в web root.

```nginx
server {
    listen 443 ssl;
    server_name jury.example.invalid;
    ssl_certificate /etc/forteguard/tls/fullchain.pem;
    ssl_certificate_key /etc/forteguard/tls/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
    root /srv/forteguard/frontend/dist;
    index index.html;

    client_max_body_size 129m;
    client_header_timeout 10s;
    client_body_timeout 60s;
    send_timeout 60s;
    keepalive_timeout 10s;
    access_log off;
    error_log /var/log/nginx/forteguard-error.log crit;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    location ~ ^/(ui-api|api/v1)/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_set_header X-Forwarded-For "";
        proxy_set_header X-Forwarded-Proto "";
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_cache off;
        proxy_max_temp_file_size 0;
        proxy_connect_timeout 5s;
        proxy_send_timeout 75s;
        proxy_read_timeout 75s;
    }
    location = /health { return 404; }
    location = /docs { return 404; }
    location = /redoc { return 404; }
    location = /openapi.json { return 404; }
    location ~ /\. { deny all; }
    location / { try_files $uri $uri/ /index.html; }
}
```

Проверки `nginx -t` и реального HTTPS **обязательны и пока не выполнены**. Пример не включает настройку default-vhost, firewall, DNS, выдачу/продление сертификата: это зависит от хоста. Не открывать стенд до их проверки.

129 MiB — внешний предел с multipart overhead; Backend проверяет свой более точный лимит и размер самого файла. Отключение request buffering вместе с HTTP/1.1 позволяет передавать поток Backend без полного предварительного spool; отключённая response buffering не складывает JPEG JSON во временный файл proxy. Сверено с [документацией proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_request_buffering), [лимитов HTTP](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size) и [HTTPS](https://nginx.org/en/docs/http/configuring_https_servers.html). Конкретный конфиг требует проверки на установленной версии.

Origin, Cookie, CSRF и `X-API-Key` проходят без подмены; CORS не включается. Cookie выставляет сам Backend по production-настройке: `Secure; HttpOnly; SameSite=Strict; Path=/ui-api`, без Domain. TLS завершается в NGINX; Backend слушает только loopback и не доверяет forwarded headers. В логах нельзя включать request/response body, cookie, key или CSRF. Инфраструктура firewall должна ограничивать соединения; приложение не является защитой от сетевого DDoS.

## 5. Приёмка до передачи URL

Проверять **только синтетикой**, затем отдельно согласованными данными:

1. Запустить все команды тестов из README; проверить production bundle без mock/секретов, только один backend PID/worker, `/health=200`. Это не ML-readiness check.
2. Проверить сертификат, имя, срок/цепочку и HTTPS без исключений браузера. С другой сети открыть UI; убедиться, что 8001/5173 недоступны, а `/api/v1/*` требует ключ. Не отправлять ключ/видео по HTTP.
3. В чистом браузере Dashboard должен быть виден сразу без входа/выхода. Оставить Network на минуту: один POST `/ui-api/session` на mount (также в StrictMode), ноль session GET, без повторов. Проверить refresh и expiry (в тестовом окружении временно TTL=60): один автоматический bootstrap при 401, без автоматической повторной загрузки и доступа к прежним заданиям.
4. Проверить Cookie flags, отклонение чужого Origin и отсутствующего CSRF, недоступность задания из второй сессии, отсутствие ключей в JS/storage/Network UI.
5. С dev/test fixture, не опубликованным production fake, проверить отрицательный, один/несколько событий, JPEG, повтор и ошибку. После подключения ML повторить реальный сценарий с проверкой временных меток и нормативных источников.
6. API жюри: синтетический MOV через поле `file` → `job_id` → `processing/done/failed`, корректный JSON/JPEG, отрицательный ответ с пустым violations. Проверить повреждённый файл, превышение body limit, неверный ключ, очередь и одновременные запросы.
7. Перезапустить во время обработки: незавершённый job станет `failed`, недавний завершённый JSON сохранится, MOV не останется. UI при следующем запросе должен автоматически создать новую анонимную сессию, сообщить об истечении прежней и не повторять upload сам. После TTL старый результат → `404`, свежий доступен через API key.
8. С моделью замерить загрузку + очередь + inference + evidence + получение результата <300 с на целевом ПК, включая несколько клиентов. Default queue=8 и timeout=240 **не гарантируют этот SLA**; настроить очередь/timeout по измерениям, оставив запас.
9. Проверить offline inference после предварительной установки весов, отсутствие облачных вызовов, свободный диск и RAM/VRAM. После хакатона удалить согласованные данные/копии по утверждённой процедуре.

## 6. Передача от ML Engineer

Нужны: утверждённый обеими сторонами `docs/ml-contract.md`; локальный adapter/factory для async `analyze_video`; веса и конфигурация с отдельными лицензиями кода/checkpoint; runtime-зависимости и железо; классы/`rule_id` с проверенной картой нормативов; несколько событий и точные evidence timestamps; измерения качества и latency на разрешённом split.

Adapter не должен блокировать event loop или игнорировать отмену: Backend timeout сам по себе не прерывает синхронный бесконечный inference/GPU-вызов. Тесты тайм-аута, отмены, offline, нескольких событий и ошибок обязательны при интеграции. Схема API жюри остаётся прежней. Нет ML/нормативов — нет заявления о готовности продукта к сдаче.

## 7. Эксплуатационные границы

MOV удаляются после завершения/ошибки; startup очищает orphan uploads собственного формата. Терминальные JSON/JPEG: default 24 ч, до 100 удалений/300 с; активные/недавние не удаляются. Retention не равен гарантированному физическому стиранию: SQLite переиспользует страницы, размер файла не уменьшается автоматически, WAL/SSD/backup требуют отдельной политики. Проверять место, не выполнять online VACUUM или удаление БД вслепую.

При нехватке диска/SQLite error снять публичный доступ к загрузкам, не подменять ошибки отрицательным вердиктом; остановить сервис, определить причину по безопасным метаданным, восстановить место/права и запустить один процесс. Не удалять чужие файлы или незавершённые данные вручную без проверки. Автоматический restart процесса не является гарантией сохранения активных заданий.
