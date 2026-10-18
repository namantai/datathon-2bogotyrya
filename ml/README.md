# ForteVision: локальный ML pipeline

Рабочий baseline — замороженный DINOv2 ViT-S/14 и обученная бинарная
Logistic v4. Этот модуль обнаруживает **кандидаты возможных нарушений**;
он не распознаёт подтверждённые виды действий, тяжесть события или правовые основания.
Checkpoint, данные и результаты обработки хранятся только локально.

## Архитектура и семантика результата

`MOV → PyAV → RGB → DINOv2 → окна признаков → Logistic → sigmoid → сглаживание → события → JPEG`.

- Backbone: `facebook/dinov2-small`, ViT-S/14, 12 блоков, 6 attention heads,
  384-мерный CLS embedding. Backbone находится в `eval()` с отключёнными градиентами.
- Preprocessing: весь кадр помещается в 224×224 с сохранением пропорций
  (bicubic letterbox), RGB, ImageNet mean/std. Это явный preprocessing проекта,
  а не стандартный center crop Hugging Face. Extraction и inference используют
  одну функцию `prepare_rgb` из [backbone.py](backbone.py).
- Стартовые параметры `DetectionConfig`: 2 FPS, окно 4 с, шаг 1 с,
  threshold 0.5, центрированное усреднение по 3 окнам, merge gap 0 с.
  **При inference используются параметры из checkpoint**, а не эти значения
  по умолчанию. Фактическая конфигурация v4 не определяется по имени файла.
- Logistic нормализует признаки сохранёнными train-only mean/std,
  усредняет последовательность внутри окна и применяет `Linear(384, 1)`.
  Эта голова не моделирует порядок кадров внутри окна.
- Дополнительная `temporal`-голова в [temporal.py](temporal.py) содержит две
  `Conv1d` и mean/max pooling. Она доступна для отдельных экспериментов;
  выбранным baseline остаётся Logistic v4.
- Видео считается положительным, если хотя бы одно **сглаженное** окно достигло
  сохранённого threshold. Перекрывающиеся окна объединяются; момент выбирается
  по окну с максимальным сглаженным score и привязывается к ближайшему
  выбранному исходному кадру. Это оценка локализации, не границы истинного действия.
- Таймкоды — секунды от первого декодированного видеокадра, с использованием PTS.
  Последний кадр учитывается. Для evidence повторно декодируется исходный MOV,
  проверяются PTS и SHA-256; сохраняются 1–3 исходных JPEG на событие.
- Score в поле `confidence` не калиброван. Ошибка модели/декодирования вызывает
  исключение; пустой список возвращается только после успешного анализа.

## Окружение и локальные модели

Команды ниже выполняются **владельцем данных в обычном терминале**, из корня
репозитория. Примеры shell совместимы с Bash/Zsh. Нужны Python 3.11,
FFmpeg/FFprobe в `PATH` и зависимости [requirements.txt](requirements.txt).
На MacBook Air M2 с 16 GB RAM начните с batch size 4; PyTorch должен поддерживать
MPS. На Linux/CPU используйте `--device cpu`; CUDA-only пакеты не требуются.

```sh
# Только если окружение ещё не создано:
python3.11 -m venv .venv
.venv/bin/python -m pip install -r ml/requirements.txt
.venv/bin/python -c 'import torch; print("torch:", torch.__version__, "MPS:", torch.backends.mps.is_available())'
ffmpeg -version
ffprobe -version
```

`--device auto` выбирает MPS, если он доступен, иначе CPU. Encoder повторяет
неподдержанную MPS-операцию на CPU и отмечает fallback. Маленькая голова по
умолчанию обучается и выполняет production scoring на CPU.

Ожидаемые локальные пути (содержимое не входит в Git):

| Артефакт | Путь |
|---|---|
| Публичный backbone и manifest проверки | `private_data/weights/dinov2-small/` |
| Выбранная обученная голова | `private_data/checkpoints/logistic-v4/head.pt` |
| Сохранённое разбиение | `private_data/checkpoints/logistic-v4/split.json` |
| История обучения | `private_data/checkpoints/logistic-v4/training.json` |

Предварительная загрузка **только публичного backbone**, если его ещё нет:

```sh
.venv/bin/python -m ml.download_weights --output private_data/weights/dinov2-small
```

Команда отказывается перезаписывать существующий каталог. Revision закреплена
в коде: `ed25f3a31f01632728cabb09d1542f84ab7b0056`. SHA-256 `model.safetensors`:
`ae1e99fcefd534ed978cdeb8326f08030c96e28b7a81ffcbc98a857c84d14be1`.
Download manifest фиксирует `Apache-2.0` для публичного DINOv2 и источник лицензии;
он не лицензирует приватную разметку или обученную на ней голову.

При загрузке проверяются manifest, хеши файлов, revision, параметры архитектуры
и точность загрузки state dict. Transformers получает `local_files_only=True`;
используются safetensors, отключаются Hub/Transformers network access и telemetry.
Inference не вызывает downloader. Недостающий snapshot — операционная ошибка.

Владелец может проверить encoder на сгенерированных пикселях без чтения видео:

```sh
.venv/bin/python -m ml.smoke_backbone --model-dir private_data/weights/dinov2-small --device auto
```

## Разметка и подготовка данных

Вход [dataset.py](dataset.py) — локальная `.xlsx`, каталог MOV и **явно указанные**
названия колонок. Пример схемы ниже синтетический; названия реальных колонок,
лист и единицы числовых таймкодов владелец проверяет локально.

| Колонка примера | Содержание |
|---|---|
| `video` | Идентификатор, соответствующий имени MOV |
| `violation` | Подтверждённая бинарная метка `1`/`0` |
| `timestamp` | Для положительных: секунды, `MM:SS`, `HH:MM:SS` или Excel time |
| `group` (необязательно) | Общая сценарная группа для связанных записей |

ID нормализуются через Unicode NFKC, верхний регистр, удаление пробелов и `.MOV`,
замену кириллической `Х` на латинскую `X`. Несколько событий разделяются `;`.
Пустая метка остаётся неизвестной; её нельзя выводить из имени или предсказания.
Для отрицательного видео timestamps должны быть пустыми. Для положительного
нужен хотя бы один корректный timestamp в пределах видео. Числовые значения
по умолчанию означают секунды; для долей Excel-суток задайте `excel_days`.

```sh
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
DATASET_RUN="private_data/manifests/validation-$RUN_TAG"

# Замените примерные путь, лист и заголовки согласно официальному источнику.
.venv/bin/python -m ml.dataset \
  --excel private_data/annotations.xlsx --videos private_data/videos \
  --sheet labels --id-column video --label-column violation \
  --timestamp-column timestamp --group-column group \
  --numeric-timestamp-unit seconds --output "$DATASET_RUN"
```

Если `group` отсутствует, уберите `--group-column`. Текстовые метки разрешаются
только через явную карту: например, после подтверждения схемы добавьте
`--positive-value 1 --positive-value violation --positive-value нарушение`
и `--negative-value 0 --negative-value no_violation --negative-value 'без нарушения'`.
Переданные варианты заменяют соответствующие значения по умолчанию.
Неподдержанные строки и формулы не становятся отрицательными метками.

Результат — `report.json` и `manifest.json` с `validation_ok`, `issue_codes`,
массивом `videos`: `video_id`, `video_path`, `label`, `timestamps_sec`, `group_id`,
`eligible`, `metadata`. Проверяются отсутствующие/дублированные ID и строки,
некорректные labels/timestamps и нечитаемые видео. Ошибки нужно разрешить
локально, сохраняя оригинальную разметку. Известные дубликаты и неизвестные
метки не включаются в supervised dataset автоматически.

## Frozen feature cache

```sh
FEATURES_NEW="private_data/features/extraction-$RUN_TAG"
.venv/bin/python -m ml.extract_features \
  --manifest "$DATASET_RUN/manifest.json" --output "$FEATURES_NEW" \
  --model-dir private_data/weights/dinov2-small \
  --sample-fps 2 --batch-size 4 --device auto
```

Каждое видео получает `features.npz` (embeddings, timestamps, indices, PTS) и
`metadata.json` (хеш источника, preprocessing/model identity, duration).
Общий `index.json` связывает cache с labels/groups. Extraction не обучает голову.
`--allow-unlabeled` разрешает пропустить неизвестные labels только когда это
единственная категория ошибок manifest; нормальные примеры из них не создаются.
Feature loader отвергает повторные video IDs и одинаковые source SHA-256,
несовместимый backbone/preprocessing и повреждённые массивы.

## Обучение: отдельный локальный эксперимент

**Для использования v4 обучение не требуется.** Следующая команда — пример
отдельного запуска владельцем данных; она не воспроизводит неизвестную историю
v4 и не должна записывать в каталог v4. Независимый final test запрещено использовать
для обучения, early stopping, подбора threshold или выбора модели.

Введите путь к существующему cache, соответствующему сохранённому split:

```sh
printf 'Existing feature cache for the saved v4 split: '
IFS= read -r FEATURES
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -m ml.train \
  --features "$FEATURES" \
  --split private_data/checkpoints/logistic-v4/split.json \
  --output "private_data/checkpoints/logistic-experiment-$RUN_TAG" \
  --kind logistic --device cpu --epochs 60 --batch-size 32 \
  --learning-rate 0.001 --patience 10 --seed 2026
```

Эта команда использует CLI defaults для конфигурации новой модели, не меняет
threshold v4. При переиспользовании split проверяется fingerprint cache.
При создании нового baseline без `--split` доступны `--val-fraction` и
`--test-fraction`; `--test-fraction 0` оставляет test пустым. Train/validation
обязаны содержать оба класса; недопустимое разбиение отклоняется. Группы и целые
видео не разделяются между выборками. Без `group_id` сценарная независимость
не гарантируется. `--kind temporal` включает альтернативную голову.

Положительными являются окна, содержащие официальный timestamp; остальные окна
положительного видео игнорируются (`-1`). Все окна подтверждённого нормального
видео отрицательны. Нормализация вычисляется только по train. Weighted BCE
балансирует суммарный вес классов и видео; optimizer — AdamW, early stopping —
по validation window loss. Сохранение проверяет ненулевые gradients и изменение
параметров. Результаты: новые `head.pt`, `split.json`, `training.json`.

## Validation и диагностика без переобучения

Здесь `FEATURES` — исходный cache с fingerprint, записанным в checkpoint;
новые независимые видео нельзя добавлять в этот cache для обхода проверки.

```sh
printf 'Existing feature cache matching Logistic v4: '
IFS= read -r FEATURES
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -m ml.evaluate \
  --features "$FEATURES" --checkpoint private_data/checkpoints/logistic-v4/head.pt \
  --split validation --output "private_data/reports/validation-$RUN_TAG.json"

.venv/bin/python -m ml.diagnose \
  --features "$FEATURES" --logistic-checkpoint private_data/checkpoints/logistic-v4/head.pt \
  --cv-folds 0 --output "private_data/diagnostics/logistic-$RUN_TAG"
```

`evaluate` возвращает Accuracy, Precision, Recall, F1, TP/TN/FP/FN, FPR/FNR,
confusion matrix и временные ошибки. `--split test` допустим только для
существующего непустого test split; threshold override для test запрещён.
Validation участвовала в early stopping и не является независимым final test.

`evaluate` CLI использует **greedy one-to-one matching без tolerance** для
localization MAE; это не event F1 при ±3 с. `diagnose` отдельно считает
one-to-one localization при ±3 с, supervised/ignored окна, распределения scores,
простые baselines и порядок кадров. С `--cv-folds 0` он не обучает модели.
Приватные per-video отчёты нельзя публиковать.

Для полного локального inference по сохранённому split добавьте к `evaluate`
`--end-to-end --model-dir private_data/weights/dinov2-small --device auto` и
укажите новый output. Это включает декодирование, encoder и evidence, но не HTTP
и очередь FastAPI. Cached-feature timing не является скоростью полного анализа.

## Inference с Logistic v4

```sh
printf 'Local development MOV path inside private_data: '
IFS= read -r VIDEO
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -m ml.inference \
  --video "$VIDEO" --checkpoint private_data/checkpoints/logistic-v4/head.pt \
  --model-dir private_data/weights/dinov2-small \
  --device auto --batch-size 4 --evidence-count 3 \
  --output "private_data/reports/inference-$RUN_TAG.json"
```

Threshold намеренно не переопределяется. CLI записывает события, detection config,
`model_load_sec`, `complete_inference_sec`, `cold_total_sec`; HTTP/очередь не входят.
Для замера владелец выбирает разрешённое development-видео нужной длительности.
Лимит жюри 5 минут нельзя объявлять выполненным без полного API benchmark на
целевом оборудовании. Выходы CLI создаются без перезаписи существующих отчётов.

## Контракт интеграции с FastAPI

Публичная Python-точка входа: `ml.inference.analyze_video(video_path: str) -> list[dict]`.
Настройки: `FORTEVISION_PRIVATE_ROOT`, `FORTEVISION_HEAD`, `FORTEVISION_MODEL_DIR`,
`FORTEVISION_EVIDENCE_ROOT`, `FORTEVISION_DEVICE`. По умолчанию используются
указанные выше пути v4. Инициализация кешируется на процесс, inference сериализован;
после смены checkpoint/config worker нужно перезапустить.

Каждый элемент содержит `type="possible_violation"`, `timecode_sec`, нейтральный
`description`, `evidence_paths`, соответствующий массив `evidence_timestamps_sec`
и внутренний некалиброванный `confidence`. `[]` означает отсутствие обнаруженных
событий. Specific action, severity и legal rule не выводятся из бинарного score.

Для backend-owned evidence есть `VideoAnalyzer.predict()`: тот же detector
возвращает `start_sec`, `end_sec` и PTS-кандидаты, без сохранения JPEG. Адаптер
должен преобразовать `type → class_id`, `description → observation`, передать
`rule_id=None` и собрать `AnalysisResult(events=...)`. Backend отвечает за
auth/upload/queue, извлечение и проверку JPEG, base64 и схему жюри:
`POST /api/v1/analyze`, `GET /api/v1/jobs/{job_id}` с `X-API-Key`.
Непустые события дают `violation`, пустые — `no_violation`; исключение — `failed`.
Fake fallback недопустим. Секрет API не передаётся в frontend.

**Граница публикации:** backend-адаптер и связанные audit/rulebook-инструменты
согласуются отдельно. Python list от `analyze_video` не является готовым
`AnalysisResult` или HTTP-ответом. Process bridge, ожидающий объект с `events`
и `class_id`, нельзя направить на эту функцию без адаптера.
[Общий контракт](../docs/ml-contract.md) может отражать другую стадию интеграции;
наличие ML-файлов само по себе не подтверждает работающую цепочку React → API → v4.

## Известное качество и ограничения

По результатам локальной проверки, **сообщённым владельцем данных**, Logistic v4
на пяти development validation-видео: Accuracy 80%, Precision 100%,
Recall 66.67%, F1 80%, TP=2, TN=2, FP=0, FN=1. В рамках подготовки этой
документации приватные отчёты не читались и метрики не пересчитывались.
Это не независимая точность и не гарантия качества на новых видео.

Event localization заметно слабее video classification по сообщению владельца;
подтверждённого численного event F1 для публикации здесь нет. Mean pooling может
размывать короткое событие, сглаживание — подавлять отдельный положительный пик,
а max-over-windows — усиливать ложные срабатывания длинных видео. Это ограничения
метода, не установленная причина ошибки конкретной записи. Viewpoint invariance,
калибровка, специфические виды нарушений и юридическая квалификация не доказаны.
Синтетические тесты проверяют программные контракты, не банковскую точность.

## Синтетические тесты

В чистом checkout опубликованной ML-ветки, с установленными зависимостями:

```sh
env -u FORTEVISION_TEST_MODEL_DIR HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  HF_HUB_DISABLE_TELEMETRY=1 .venv/bin/python -m pytest \
  -o addopts= -p no:cacheprovider -q tests/ml
```

Фикстуры создают массивы, MOV и XLSX во временных каталогах. Два теста реального
pretrained encoder пропускаются без `FORTEVISION_TEST_MODEL_DIR`; не задавайте
этот путь в агентской сессии. Незакоммиченные интеграционные тесты в рабочем дереве
могут требовать ещё не опубликованный backend — их результаты нельзя считать
проверкой независимого ML snapshot.

Оригинальные видео/разметка, embeddings, JPEG, checkpoints, private reports и
секреты остаются под локальной защитой `.gitignore`. Не передавайте их агенту,
в Git, CI artifacts или внешние сервисы. Held-out данные не используются для
выбора моделей и threshold. Все реальные команды выше запускает только владелец.
