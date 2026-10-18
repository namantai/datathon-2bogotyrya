# TASK D02: локальный Docker-стенд ForteGuard AI

Это **локальный HTTP development-стенд**, не публичный production deployment.
Два контейнера: React static build → NGINX:8080 → FastAPI:8000 → существующие
SQLite/worker/FFmpeg. Только NGINX публикуется на **127.0.0.1:8080**.
Backend не имеет host port и подключён только к Docker-сети `internal: true`.
Frontend подключён также к обычной bridge-сети для публикации порта.
Инференс по-прежнему локальный; никакого внешнего AI или нового ML-сервиса нет.

## Режимы и границы

- `compose.yaml`: `APP_ENVIRONMENT=development`, анализатор **disabled** независимо
  от значения в локальном `.env`. Dashboard работает, но анализ завершается
  `failed`, а не выдуманным результатом. `/health` проверяет worker, **не готовность ML**.
- `compose.yaml` + `compose.local-fake.yaml`: явно включён **development FakeAnalyzer**.
  Подходит только для проверки транспорта на синтетике; `no_violation` — заглушка,
  не результат настоящего AI. UI показывает fake-режим.
- Сам Backend-образ по умолчанию использует `production` + `disabled`; приложение
  отклоняет `production` + `fake`. Класс FakeAnalyzer остаётся в существующем коде,
  но не должен выполняться в production. Production-конфигурация здесь не создаётся.

NGINX раздаёт SPA и проксирует `/api/*`, `/ui-api/*`, `/health`, сохраняя путь,
`Origin`, cookies, CSRF и `X-API-Key`. В runtime-образе Frontend нет Node/Vite server.
Docker DNS повторно разрешается после пересоздания Backend. NGINX не пишет
request/access-логи и не буферизует тела/ответы на диск. Лимит NGINX — 129 MiB;
Backend — файл 128 MiB плюс отдельная проверка multipart overhead. Эти лимиты
зафиксированы в Compose и NGINX и меняются **согласованно**, не только в `.env`.
PID и все HTTP temp paths NGINX перенесены в writable `/tmp` для non-root/read-only
запуска согласно [рекомендациям NGINX](https://github.com/nginx/docker-nginx-unprivileged#troubleshooting-tips).

Авторизация API жюри, JSON и маршруты не изменены. Dashboard не требует входа;
автоматическая HttpOnly / SameSite=Strict cookie и CSRF/Origin/rate limiting остаются.
Для **локального HTTP** Secure-флаг cookie выключен существующим development-режимом;
в production он обязателен и включается приложением. Не публикуйте этот стенд наружу.

## 1. Подготовка на Arch Linux (Fish)

Все команды ниже — из корня проекта; Docker Engine должен быть запущен, Compose v2+
доступен через `sudo docker compose version`. Используется `sudo`, не изменение прав socket.

```fish
cd /home/mrturtle/datathon/datathon-2bogotyrya
sudo docker version
sudo docker compose version
# Существующий backend/.env НЕ перезаписываем.
if not test -f backend/.env
    cp .env.example backend/.env
end
chmod 600 backend/.env
```

Если `.env` только создан: в локальном редакторе замените placeholder `APP_API_KEY`
случайным секретом ≥16 символов (желательно 32 случайных байта из менеджера паролей).
Не вводите секрет прямо в shell command/history и не показывайте содержимое файла.
Ручного UI-пароля нет. Compose передаёт файл **только Backend при запуске**, не при сборке.
Переменные путей хранилища/FFmpeg, origin, режима и размера файла переопределяются
Compose для контейнерного окружения; остальные поддерживаемые настройки берутся из файла.

Можно указать другой приватный файл: `sudo env FORTE_ENV_FILE=/absolute/private/env docker compose …`.
Передавайте тот же путь и smoke-сценарию. `sudo` может очищать экспортированные переменные Fish.
Не запускайте обычный `docker compose config` в публичном терминале/логе: он раскрывает
значения runtime-переменных. Для проверки используйте **`config --quiet`**.

## 2. Сборка и запуск

Проверка и сборка (скачивание базовых образов и пакетов требует интернета):

```fish
sudo docker compose config --quiet
sudo docker compose -f compose.yaml -f compose.local-fake.yaml config --quiet
sudo docker compose build
```

Запуск без заглушки, одной командой:

```fish
sudo docker compose up --build -d --wait
```

Для **синтетического полного smoke**, вместо предыдущей команды:

```fish
sudo docker compose -f compose.yaml -f compose.local-fake.yaml up --build -d --wait
sudo docker compose ps
curl --fail http://localhost:8080/health
```

Откройте [http://localhost:8080](http://localhost:8080). Dashboard появляется без
пароля/регистрации/выхода. Используйте один hostname последовательно: cookies
`localhost` и `127.0.0.1` относятся к разным посетителям.

## 3. Проверки API, SQLite и браузера

После запуска fake-стенда выполните **только на синтетике, без чужих активных заданий**:

```fish
sudo python3 scripts/docker_smoke.py --restart
sudo docker compose ps
```

Скрипт работает только с `http://localhost:8080`, проверяет development/fake,
использует установленный в контейнере FFmpeg для создания синтетического MOV,
получает ключ из контейнера только в память, не выводит его и не передаёт в argv.
Проверяет сайт/SPA, health, Origin, CSRF, HttpOnly cookie/reuse, двух посетителей,
отказ без/с неверным ключом на **обоих** маршрутах жюри, загрузку через поле `file`
для UI и API, bounded polling и результат через настоящий Backend/SQLite/worker.
Проверяет, что Settings production отклоняет fake.

`--restart` перезапускает **только Backend**, прерывает текущую работу и сбрасывает
UI-сессии; затем проверяет сохранность завершённого JSON через API жюри и изоляцию
новой UI-сессии. Без этого флага рестарт не выполняется. Скрипт оставляет только
синтетические результаты под обычной политикой retention, временный MOV удаляется.
Ни успех HTTP smoke, ни `healthy` не подтверждают качество модели.

Ручная проверка в браузере (скрипт не исполняет React):

1. DevTools → Network → фильтр `/ui-api/session`; открыть/обновить страницу.
   Должен быть один автоматический POST на монтирование, **ноль GET**, без повторов
   во время ожидания/рендеров. В production bundle StrictMode не дублирует эффекты;
   существующий regression test отдельно проверяет development StrictMode.
2. Dashboard виден сразу. Загрузить свой **синтетический** MOV: `202` → polling →
   результат с явной маркировкой FakeAnalyzer. Не использовать видео жюри/реальные данные.
3. В другом профиле/приватном окне получить отдельную сессию: чужой UI job возвращает `404`.
4. Обновить страницу: существующая cookie переиспользуется. После restart/TTL
   новая сессия создаётся автоматически; старые UI jobs не присваиваются ей.
   Восстановления истории после restart нет; API жюри читает сохранённый результат по ключу.

Проверки без Docker daemon (при установленных зависимостях):

```fish
cd backend
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q src tests ../scripts/docker_smoke.py
.venv/bin/python -m pip check
cd ../frontend
npm test
npm run typecheck
npm run build
npm run test:production
cd ..
git diff --check
```

`test_docker_config.py` использует только Compose CLI и **синтетический env-файл**;
проверяет изоляцию, режимы, volume, runtime-only секрет и build policy. При отсутствии
CLI/plugin три Compose-теста пропускаются, статический тест остаётся. Это **не сборка образа**.

## 4. Проверка образов и лицензий после сборки

```fish
sudo docker compose exec -T frontend nginx -t
sudo docker compose exec -T backend python --version
sudo docker compose exec -T backend ffmpeg -version
sudo docker compose exec -T backend ffprobe -version
sudo docker compose exec -T backend ffmpeg -L
sudo docker compose exec -T backend python -m pip check
sudo docker run --rm --network none --entrypoint python (sudo docker compose images -q backend) -c 'import os,pathlib; assert "APP_API_KEY" not in os.environ; assert not list(pathlib.Path("/app").rglob(".env*")); assert not list(pathlib.Path("/var/lib/forteguard").rglob("*.sqlite*")); print("Clean runtime env/data defaults")'
sudo docker run --rm --network none --entrypoint sh (sudo docker compose images -q frontend) -c 'test -z "$APP_API_KEY" && ! command -v node && ! find /usr/share/nginx/html -name ".env*" | grep -q .'
```

`.dockerignore` — allowlist исходников/манифестов; `.env`, Git, реальные видео/кадры,
SQLite, логи, веса, ML, node_modules и venv не отправляются в build context. Dockerfiles
используют явные COPY, не `COPY . .`, и не используют секреты в ARG/ENV/RUN.
Эти smoke-проверки проверяют отдельные инварианты файловой системы и env свежего
образа без volume, **не заменяют полный аудит всех слоёв/SBOM** перед распространением.
Не публикуйте вывод `docker inspect` контейнера, env или полный Compose config: ключи
доступны Docker-администратору в runtime metadata. `.env` не является secret vault.

Используются официальные Python/Node/NGINX images и уже существующие зависимости.
[Python — PSF](https://docs.python.org/3.11/license.html),
[NGINX — BSD](https://nginx.org/LICENSE),
[Docker Node packaging — MIT](https://github.com/nodejs/docker-node/blob/main/LICENSE).
Node применяется только для сборки; лицензии самих пакетов и ОС также сохраняются.
[FFmpeg — LGPL/GPL в зависимости от build flags](https://ffmpeg.org/legal.html):
GPL допускает коммерческое использование, но при распространении образов нужно
выполнить условия предоставления соответствующих исходников/notices. До публикации
проверить фактический `ffmpeg -L/-buildconf`, зависимости и лицензии каждого образа;
веса и их лицензии в эту задачу не входят. Notices из системных пакетов не удаляются.

## 5. Логи, остановка, обновление, сохранение данных

```fish
sudo docker compose logs --tail=100 -f backend frontend
# Остановить, не удаляя контейнеры или данные:
sudo docker compose stop
# Повторно запустить тот же локальный fake-режим:
sudo docker compose -f compose.yaml -f compose.local-fake.yaml up -d --wait
# После согласованного обновления исходников: пересобрать и пересоздать контейнеры:
sudo docker compose -f compose.yaml -f compose.local-fake.yaml up --build -d --wait
# Удалить только контейнеры/сети, volume оставить:
sudo docker compose down
```

Для режима без fake используйте только `compose.yaml` во всех командах `up`.
Никогда не используйте `down -v`, `volume rm`, `volume prune` для нужного хранилища.
Volume **`forteguard_backend-data`** содержит `jobs.sqlite3` (и WAL/SHM), `uploads/`
и `tmp/`. Инициализируется владельцем UID/GID 10001; приложение работает не от root.
Не меняйте имя проекта (`-p`/`COMPOSE_PROJECT_NAME`) при перезапуске: другое имя создаёт
другой volume, а не переносит старые результаты. Хостовый `backend/storage` не монтируется
и существующая dev-база в этот volume **не мигрирует**.

Перезапуск сохраняет недавние terminal JSON, но незавершённые задания становятся
`failed`, in-memory UI sessions теряются. MOV очищаются после обработки; JSON/JPEG
по умолчанию живут 24 часа. Это не бессрочный архив. Для резервной копии используйте
SQLite online backup API или остановите Backend и сохраните **весь volume** приватно;
нельзя копировать только живой `jobs.sqlite3`, игнорируя WAL. Контролируйте свободный диск
и остаточные multipart temp-файлы после аварийного kill; volume сам по себе не имеет квоты.

## 6. Возможные ошибки

- `permission denied docker.sock`: выполнять через `sudo` в локальном терминале;
  Codex не меняет права socket и не запрашивает пароль в чат.
- `backend/.env not found`, короткий/пустой ключ: подготовить приватный файл, без утечки в логи.
- `port is already allocated`: проверить, кто использует 8080; не завершать чужие процессы.
- `403` UI: hostname/port/Origin должен точно соответствовать разрешённым; не отключать CSRF.
- `429`: действуют rate limits; подождать минуту, не запускать smoke многократно подряд.
- `413`: размер файла превышает 128 MiB или multipart/proxy bound; согласовать оба лимита.
- `502`/unhealthy: проверить логи, FFmpeg, права UID 10001 на volume, настройки и readiness
  Backend; после смены IP Docker DNS может обновляться до 5 секунд. Не лечить `chmod 777`.
- `failed` при healthy: в базовом Compose это ожидаемо с `disabled`; либо реальная ошибка
  обработки. Fake включается только отдельным локальным overlay, не как production fallback.
- Build без сети: базовые образы/npm/PyPI/apt должны быть доступны заранее. Runtime Backend
  изолирован от интернета; загрузки моделей во время обработки не предусмотрены.

## 7. MacBook, arm64 и финальный deployment

Платформа не зафиксирована как amd64: используются multi-arch базовые образы;
на Apple Silicon Compose должен собирать нативный Linux arm64. Фактическая arm64-сборка
в этой задаче **не проверена**. Для отдельной проверки на подходящем Docker builder:

```fish
sudo docker buildx build --platform linux/arm64 --load -f backend/Dockerfile -t forteguard-backend:arm64 .
sudo docker buildx build --platform linux/arm64 --load -f frontend/Dockerfile -t forteguard-frontend:arm64 .
```

На macOS обычно используется Docker Desktop, `sudo` там может не требоваться.
PyTorch MPS/Metal хоста **не предполагается доступным в Linux VM/контейнере**.
В образах нет PyTorch, весов или ML-модуля. Точка интеграции `create_app(analyzer=...)`
и DRAFT ML-контракт не изменены. До интеграции согласовать CPU/Linux-совместимость,
лицензии code/checkpoint и замер на MacBook. Если нужен macOS-native MPS inference,
архитектуру согласовать отдельно; межконтейнерный ML-сервис автоматически не добавляется.

Перед публичным deployment нужны: реальный локальный analyzer, лицензии, измерение
полного пути ≤5 минут на целевом железе; отдельно проверенный production override
с `APP_ENVIRONMENT=production`, HTTPS/TLS, точными HTTPS origins/Secure cookies,
приватным ключом, firewall, backups/retention/квотами и ограничениями ресурсов;
тест из другой сети. Backend по-прежнему не публиковать. Зафиксировать проверенные
image digests и версии Python-пакетов: сейчас base tags и существующие pip ranges
не дают bit-for-bit воспроизводимости. Выполнить image/layer/dependency audit.
Не использовать local-fake overlay в production.

## Фактически проверено при подготовке D02

На хосте: Backend **119 passed** в существующем Python 3.14 окружении и **119 passed**
в отдельно установленном Python 3.11.16; `pip check` в обоих окружениях без ошибок.
Первый Python 3.11 pytest внутри sandbox дал 15 ошибок вокруг async FFmpeg/FFprobe;
повтор **без изменений кода вне sandbox** прошёл полностью (9.80 с). Это различие
окружений, не основание ослаблять тайм-ауты/защиты или объявлять Docker проверенным.
HTTP E2E на Python 3.11 с временным Vite proxy и настоящим FastAPI прошёл: синтетический
MOV, сессии/CSRF/изоляция/API key, processing/done/failed, ноль/одно/несколько событий,
реальные JPEG синтетических кадров и SQLite. Это не NGINX и не браузерная проверка.
Frontend: **86 passed**, включая StrictMode/ограничение session-запросов; typecheck,
production build, проверка отсутствия mock/ключей в bundle — успешны. `compileall`,
синтаксис Fish-блоков (`fish --no-execute`) и `git diff --check` — без ошибок.
OpenAPI hash до/после совпадает; исходники Backend/Frontend, ML и AGENTS.md не менялись.

Compose CLI доступен, обе конфигурации проходят `config --quiet`. Docker daemon
недоступен процессу Codex (`docker.sock: permission denied`; `sudo -n` требует пароль).
Поэтому **build/up/ps, работа NGINX в контейнере, container smoke, restart volume,
содержимое собранных image layers и arm64 не подтверждены**. Их нужно выполнить локально
командами выше. До этого TASK D02 — **PARTIAL**, не PASS.

Основания конфигурации:
[Compose runtime env_file](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/),
[build context/.dockerignore](https://docs.docker.com/build/concepts/context/),
[persistent volumes](https://docs.docker.com/engine/storage/volumes/),
[Compose networking/DNS](https://docs.docker.com/compose/how-tos/networking/),
[NGINX proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass).
