import type { Phase } from "../hooks/useAnalysis";
import { Icon } from "./Icon";
export function AnalysisStatus({
  phase,
  jobId,
  demo,
}: {
  phase: Phase;
  jobId: string;
  demo: boolean;
}) {
  const index =
    phase === "done"
      ? 3
      : phase === "processing"
        ? 2
        : phase === "uploading"
          ? 1
          : 0;
  return (
    <aside className="panel status-card" aria-labelledby="status-title">
      <div className="section-heading">
        <span className="section-number">02</span>
        <h2 id="status-title">Ход проверки</h2>
      </div>
      <ol className="steps">
        {[
          "Подготовка записи",
          "Загрузка видео",
          "Анализ записи",
          "Результат проверки",
        ].map((label, i) => (
          <li
            key={label}
            className={
              phase === "failed"
                ? ""
                : index > i || phase === "done"
                  ? "complete"
                  : index === i
                    ? "current"
                    : ""
            }
          >
            <span className="step-dot">
              {index > i || phase === "done" ? (
                <Icon name="check" size={14} />
              ) : (
                i + 1
              )}
            </span>
            <div>
              <strong>{label}</strong>
              <small>
                {
                  [
                    "Выберите MOV-файл",
                    demo
                      ? "Демонстрация загрузки"
                      : "Передача в локальный сервис",
                    "Ожидаем завершения",
                    "События и кадры",
                  ][i]
                }
              </small>
            </div>
          </li>
        ))}
      </ol>
      <div className="status-message" role="status" aria-live="polite">
        {(phase === "uploading" || phase === "processing") && (
          <span className="spinner" aria-hidden="true" />
        )}
        {phase === "uploading"
          ? demo
            ? "Демонстрация загрузки…"
            : "Загрузка видео…"
          : phase === "processing"
            ? "Обработка записи…"
            : phase === "done"
              ? "Проверка завершена"
              : phase === "failed"
                ? "Анализ не завершён"
                : "Готово к новой проверке"}
      </div>
      {jobId && (
        <p className="job-id">
          ID задания<span>{jobId}</span>
        </p>
      )}
      <div className="time-note">
        <Icon name="clock" size={17} />
        <p>
          Ожидание ограничено 5 минутами. Время зависит от длины записи и
          нагрузки на сервис.
        </p>
      </div>
    </aside>
  );
}
