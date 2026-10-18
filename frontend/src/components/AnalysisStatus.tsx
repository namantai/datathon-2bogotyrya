import { useEffect, useState } from "react";
import { ANALYSIS_TIMEOUT_MS, formatClock } from "../lib/analysis";
import { Icon } from "./Icon";

export function AnalysisStatus({
  startedAt,
  onCancel,
}: {
  startedAt: number;
  onCancel: () => void;
}) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(performance.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const elapsed = Math.min(ANALYSIS_TIMEOUT_MS, Math.max(0, now - startedAt));
  const label = `${formatClock(elapsed / 1000)} / ${formatClock(ANALYSIS_TIMEOUT_MS / 1000)}`;
  return (
    <section className="panel working-card" aria-labelledby="analysis-title">
      <span className="state-icon">
        <Icon name="clock" size={28} />
      </span>
      <h1 id="analysis-title" tabIndex={-1}>
        Идёт анализ видео...
      </h1>
      <p className="lead">Запись обрабатывается. Дождитесь результата.</p>
      <div className="elapsed-heading">
        <span>Время проверки</span>
        <span
          className="timer"
          role="timer"
          aria-label="Прошло времени / лимит"
        >
          {label}
        </span>
      </div>
      <progress
        value={elapsed}
        max={ANALYSIS_TIMEOUT_MS}
        aria-label="Прошедшее время относительно лимита"
        aria-valuetext={label}
      />
      <p className="muted time-explanation">
        Полоса показывает прошедшее время, а не готовность AI.
      </p>
      <button className="text-button" onClick={onCancel}>
        Отменить ожидание
      </button>
    </section>
  );
}
