# TASK 06A — локальная проверка готовности

Исторический отчёт TASK 06A ниже описывает состояние на момент его выполнения. Последующее задание удалило ручной UI-login: текущий протокол — автоматический POST bootstrap анонимной сессии, без session GET/DELETE и UI-кода. Актуальные запуск/защита и проверки описаны в README компонентов; старые численности тестов здесь не являются результатами нового задания.

Дата: 18 октября 2026, ветка `feature/fullstack`. Рабочее дерево до задачи было чистым. AGENTS, ML-код, ML-контракт и официальный HTTP-контракт не изменялись; commit/push/merge не выполнялись. Все медиа для проверки синтетические, временные и локальные.

## Аудит и изменения

| Уровень | Подтверждённый риск | Результат |
|---|---|---|
| CRITICAL для сдачи | Настоящего ML/проверенной нормативной карты нет | Известный внешний блокер; fake запрещён в production, модель не имитируется |
| HIGH | FastAPI разбирал jury multipart до Depends с проверкой ключа; до разбора не было полного body/concurrency/time лимита | Middleware проверяет ключ до чтения, ограничивает фактический поток, время и число uploads; публичная OpenAPI не меняется |
| HIGH | Остановленная worker-task оставляла queued job в `processing`, новые uploads принимались | GET финализирует недоступное worker задание как `failed`, новый submit/health возвращают `503`, shutdown безопасно обрабатывает завершившуюся с ошибкой task |
| HIGH | JPEG/base64 в SQLite хранились бессрочно | Настраиваемый TTL только терминальных записей, bounded batch, индекс; недавние/активные не удаляются |
| MEDIUM | После сбоя до регистрации upload в БД оставался MOV | Startup удаляет orphan-файлы собственного формата в отдельном storage, не обходит симлинки/подпапки |
| MEDIUM | FFprobe при отмене не завершал subprocess; форматы/протоколы декодеров не были явно ограничены | Cancel kill/reap, whitelist MOV + file/pipe во всех входных FFprobe/FFmpeg-командах |
| MEDIUM | Default очередь/timeout допускают ожидание >5 минут; недоступен runtime HTTPS | Осталось проверить после ML и на целевом публичном стенде; гарантий SLA нет |
| LOW | Корневой README описывал этап 04 без уже работающего bridge | Обновлены статус, Fish-команды, настройки/ограничения и план deployment |

Три новых регрессии **до исправлений падали**: unauthorized body был прочитан, stopped worker возвращал processing, orphan оставался. После исправлений прошли. Дополнены проверки body overflow/timeout/concurrency и закрытия частично разобранного multipart, retention/повторного использования SQLite-страниц, cancel FFprobe и неожиданного падения worker. Неверный non-ASCII API key теперь тоже даёт `401`: сравнение выполняется по bytes, без `compare_digest(str)` TypeError.

Авторизацию UI не переписывали: воспроизводимой проблемы session storm не найдено. Существующий тест проверяет 1 session GET при обычном mount / 2 при StrictMode, 20 rerender и 60 секунд виртуального ожидания без роста, успешный POST, remount и expiry. Это автоматическая проверка React/jsdom, не новая ручная проверка Chrome.

## Фактические проверки

- Backend: `python -m pytest` — **110 passed**; `compileall -q src tests` — exit 0; `pip check` — No broken requirements found. Нефатальное предупреждение pip о недоступном cache, не конфликт зависимостей.
- Frontend: typecheck, build, test:production — exit 0; Vitest — **75 passed / 5 files**; `npm audit --audit-level=high` — **0 vulnerabilities** на момент запуска. Dependency audit не заменяет аудит приложения.
- HTTP E2E: временные FastAPI:8002 и Vite:5174, настоящий proxy/cookiejar, login/CSRF, processing, no_violation, одно/два события, JPEG, failed, повтор, logout — PASS. Тестовые серверы остановлены, их временные данные удалены. Пользовательские серверы не останавливались.
- HTTP benchmark: **9 done из 9**, правильные JSON/JPEG, после каждого задания upload удалён. Настоящий jury API с ключом, не только ASGI mock.
- OpenAPI до/после полностью совпадает по SHA-256 sorted JSON: `2c7bf00f28fa339164aac6cffe7763778e154a17c55cb5523e155f5a8fd9c682`. Публичные paths: `/api/v1/analyze`, `/api/v1/jobs/{job_id}`, `/health`; `/ui-api` не публикуется в схеме.

Security coverage существующих и новых тестов: ключи/поля/enum, MOV/реальный JPEG, лимиты/невалидный ввод, production fake rejection, Origin/CSRF/cookie/session ownership, отсутствие секретов в production bundle, рестарт и очистка. Никакие новые зависимости, веса или облачные inference-вызовы не добавлялись.

Синтаксис семи Fish-блоков README проверен `fish --no-execute`; команды установки/production из документации не исполнялись.

Поиск в runtime-коде не нашёл облачных AI endpoints/SDK; единственный browser fetch — относительный `/ui-api`. Поиск характерных токенов/private-key markers в изменяемых коде/тестах/docs не нашёл секретов. Это статический поиск, не доказательство отсутствия любых секретов. `.env`, storage, dist, venv исключены Git; отслеживаемых MOV/кадров/весов/БД нет. `git diff --check` прошёл. AGENTS, ML, контракты, frontend runtime и существующие frontend tests без diff.

## Изменённые файлы

- Конфигурация: `.env.example`, `backend/src/fortebank_api/config.py`.
- Приём API: `backend/src/fortebank_api/jury_ingress.py` (новый), `auth.py`, `analysis_service.py`, `main.py` в том же пакете.
- Lifecycle/декодирование: `backend/src/fortebank_api/jobs.py`, `storage.py`, `video_validation.py`, `evidence.py`.
- Проверки: `backend/tests/test_readiness.py` и `backend/tests/benchmark.py` (новые), `backend/tests/http_e2e.py`.
- Документация: `README.md`, `backend/README.md`, `frontend/README.md`, `docs/architecture.md`, новые `docs/deployment.md` и `docs/readiness-report.md`.

## Производительность: измерено, не прогноз качества ML

Метод: `PYTHONPATH=src python -m tests.benchmark` из backend. FFmpeg `testsrc2`, 640×360, 30 FPS, MPEG-4 в MOV без аудио; 10/60/145 с, по 3 последовательных HTTP запроса. FakeAnalyzer без искусственной задержки; два тестовых события на 25%/75% ролика, по три настоящих JPEG около таймкода. Исходные кадры только синтетические. Входная генерация, startup и сборка multipart в клиенте **не входят** в HTTP-таймер. Polling каждые 20 ms; получение и JSON parsing последнего GET входят. Прогрев отдельно не выделен, машина не изолировалась от других локальных процессов.

CPU i5-12450H, 12 logical CPUs, ~15.3 GiB RAM. GPU не использовался; peak RAM/VRAM не измерялись. Значения ниже — медианы трёх прогонов, секунды; медианы независимы, складывать их как один прогон нельзя.

| MOV | Размер, bytes | HTTP POST | FFprobe внутри POST | Upload/validation на Backend | Queue | FakeAnalyzer | JPEG extraction | Полный HTTP → GET done | Максимум полного цикла |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 с | 2 305 276 | 0.045608 | 0.035494 | 0.043876 | 0.001253 | 0.000039 | 0.329181 | 0.397233 | 0.404865 |
| 60 с | 13 913 954 | 0.062232 | 0.035485 | 0.060716 | 0.000949 | 0.000031 | 0.370193 | 0.442963 | 0.459454 |
| 145 с | 33 625 248 | 0.102766 | 0.036770 | 0.101047 | 0.001070 | 0.000035 | 0.370746 | 0.507051 | 0.524213 |

Upload/validation включает multipart parsing, запись и FFprobe; отдельный FFprobe замеряется обёрткой только в test factory. Это не скорость WAN. Чистая сеть и запись не разделены инструментально. Внутренний Backend total median: 0.380725 / 0.430293 / 0.490396 с соответственно.

При отсутствии очереди наблюдаемый немодельный overhead оставляет арифметически ~299.48 с до 300 с. **Это не доступный production timeout для ML:** при текущем `APP_ANALYSIS_TIMEOUT_SEC=240` модель и evidence вместе ограничены 240 с; стоит планировать ещё запас, например начинать с целевого ML ≤230 с, затем измерять end-to-end. Внешняя загрузка, другой codec/resolution, RAM/GPU и очередь могут существенно изменить результат. При 8 ожидающих jobs и долгом inference пяти­минутный лимит не гарантирован. Необходимо подобрать ёмкость/timeout по измерениям реальной модели и конкуренции.

## SQLite, очистка и restart

В benchmark JSON с шестью JPEG занимает ~170–176 тыс. bytes/result; SQLite после трёх результатов: 544 768 / 532 480 / 532 480 bytes. Base64 увеличивает JPEG примерно в 4/3 раза, объём растёт с числом/размером кадров, реальные видео могут дать существенно больше. Нельзя оценивать сутки хранения только по этой синтетике.

Тест с 800 000 bytes синтетической нагрузки подтвердил освобождение >100 SQLite-страниц и повторное использование без роста page_count. Удаление записей не уменьшает размер файла ОС автоматически; намеренно не добавлялся blocking online VACUUM. Cleanup: terminal completed_at старше TTL, до 100 записей при старте/каждые 300 с; default TTL 86400 с — командное решение до согласования. При большом backlog удаление может запаздывать; свободное место контролировать отдельно.

Недавние done переживают restart и читаются по API key. Queued/running при restart → failed, видео удаляются. UI-сессии in-memory и при restart теряются; это сохранённая политика, не потеря БД. Несколько uploads ограничены ingress и bounded очередью; обработка последовательна. Ошибки анализатора/evidence не становятся `no_violation`. Умерший worker даёт failed/503, нужен операторский restart. Зависший синхронный будущий ML нельзя считать защищённым только asyncio timeout.

## Оставшиеся проверки / статус

**PARTIAL для общей готовности к сдаче.** Локальные автоматические проверки и HTTP-синтетика прошли; публичный API сохранён, безопасность не отключена. [Deployment plan](deployment.md) конкретизирует HTTPS, systemd, Origin/cookies, ресурсы и checklist, но NGINX-конфиг, сертификат и доступ из другой сети ещё не проверены в runtime.

Нет новой browser E2E на реальном Chrome, TLS/NGINX runtime, ML/правовой карты, целевого GPU и реального SLA/quality. Не публиковались URL, не передавались реальные видео и не запускались тяжёлые стресс-тесты. Следующий шаг: принять от ML Engineer контракт/адаптер/лицензированные локальные веса/карту правил/метрики, затем отдельно согласовать и проверить HTTPS-стенд по checklist.
