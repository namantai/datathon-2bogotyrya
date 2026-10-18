import { useEffect, useRef, useState } from "react";
import { jpegSource, safeSourceUrl } from "../lib/contracts";
import type {
  EvidenceFrame,
  ResultViewModel,
  Severity,
} from "../lib/contracts";
import { formatClock, formatTime } from "../lib/analysis";
import { Icon } from "./Icon";
import { HumanReview } from "./HumanReview";
const severityLabels: Record<Severity, string> = {
  undetermined: "не определена",
  low: "низкая",
  medium: "средняя",
  high: "высокая",
  critical: "критическая",
  unverified: "не подтверждена",
};
function Frame({
  frame,
  demo,
  index,
}: {
  frame: EvidenceFrame;
  demo: boolean;
  index: number;
}) {
  const [broken, setBroken] = useState(false);
  const source = jpegSource(frame.image_base64);
  return (
    <figure>
      {source && !broken ? (
        <img
          src={source}
          alt={`${demo ? "Синтетический тестовый кадр" : "Кадр-доказательство"} ${index + 1}, ${formatTime(frame.timestamp_sec)}`}
          onError={() => setBroken(true)}
        />
      ) : (
        <div className="image-error" role="alert">
          Кадр недоступен или повреждён. Доказательство требует проверки.
        </div>
      )}
      <figcaption>
        <span>{demo ? "Синтетический кадр" : `Кадр ${index + 1}`}</span>
        <time>{formatTime(frame.timestamp_sec)}</time>
      </figcaption>
    </figure>
  );
}
export function Results({
  model,
  demo,
  testMode = demo,
  file,
  onReset,
}: {
  model: ResultViewModel;
  demo: boolean;
  testMode?: boolean;
  file?: File | null;
  onReset: () => void;
}) {
  const positive = model.result.verdict === "violation";
  const video = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const [url, setUrl] = useState<string>();
  const [playbackError, setPlaybackError] = useState(false);
  useEffect(() => {
    if (!file) return;
    const objectUrl = URL.createObjectURL(file);
    const player = video.current;
    pendingSeek.current = null;
    setUrl(objectUrl);
    setPlaybackError(false);
    return () => {
      player?.pause();
      player?.removeAttribute("src");
      player?.load();
      URL.revokeObjectURL(objectUrl);
    };
  }, [file]);
  function seek(seconds: number) {
    const player = video.current;
    if (!player || playbackError) return;
    if (player.readyState < 1) {
      pendingSeek.current = seconds;
      return;
    }
    try {
      player.currentTime = Number.isFinite(player.duration)
        ? Math.min(seconds, player.duration)
        : seconds;
      pendingSeek.current = null;
      player.focus();
    } catch {
      setPlaybackError(true);
    }
  }
  return (
    <section className="results" aria-labelledby="results-title">
      <div className="results-top">
        <h1 id="results-title" tabIndex={-1}>
          Анализ завершён
        </h1>
        <button className="secondary" onClick={onReset}>
          Проверить другое видео
        </button>
      </div>
      {demo && (
        <p className="demo-result">
          Демонстрационный результат · события и изображения синтетические
        </p>
      )}
      <div
        className={`verdict ${positive ? "attention" : "clear"}`}
        role="status"
      >
        <span className="verdict-icon">
          <Icon name={positive ? "shield" : "check"} size={26} />
        </span>
        <div>
          <h2>
            {testMode
              ? positive
                ? "Тестовый результат: найдены события"
                : "Тестовый результат: событий нет"
              : positive
                ? "Обнаружены признаки возможных нарушений"
                : "Нарушения не обнаружены"}
          </h2>
          <p>
            {positive
              ? `Событий для просмотра: ${model.result.violations.length}. Изучите описание и кадры каждого события.`
              : "Список обнаруженных событий пуст."}
          </p>
        </div>
      </div>
      <p className="result-note">
        {testMode
          ? "Тестовый вывод не оценивает содержание видео."
          : "Результат требует проверки человеком и не является юридическим заключением."}
      </p>
      {file && (
        <section
          className="panel video-card"
          aria-label="Просмотр загруженного видео"
        >
          <div className="video-heading">
            <Icon name="video" />
            <span>{file.name}</span>
          </div>
          <video
            ref={video}
            src={url}
            controls
            muted
            playsInline
            preload="metadata"
            tabIndex={0}
            aria-label="Видеозапись проверки"
            hidden={playbackError}
            onError={() => setPlaybackError(true)}
            onLoadedMetadata={() => {
              if (pendingSeek.current !== null) seek(pendingSeek.current);
            }}
          />
          {playbackError && (
            <p className="player-fallback" role="status">
              Браузер не поддерживает воспроизведение этого MOV. Просмотрите
              кадры-доказательства ниже, если они есть в результате.
            </p>
          )}
        </section>
      )}
      {model.result.violations.map((event, index) => {
        const detail = model.result.eventDetails?.[index];
        const legacySource = model.sources?.[index];
        const sources = (
          detail?.sources ?? (legacySource ? [legacySource] : [])
        ).filter(
          (source) => source.verified === true && safeSourceUrl(source.url),
        );
        const severity = detail?.severity ?? "undetermined";
        return (
          <article
            className="panel event-card"
            key={`${model.result.job_id}-${index}`}
          >
            <div className="event-heading">
              <span className="event-index">
                {String(index + 1).padStart(2, "0")}
              </span>
              <h3>{detail?.name ?? event.type}</h3>
              <button
                className="timecode"
                disabled={!file || playbackError}
                aria-label={`Перейти к событию ${index + 1}, ${formatClock(event.timecode_sec)}`}
                onClick={() => seek(event.timecode_sec)}
              >
                <Icon name="clock" size={15} />
                {formatClock(event.timecode_sec)}
              </button>
            </div>
            <p className="severity">
              Критичность: {severityLabels[severity]}{" "}
              <span>
                {severity === "undetermined" || severity === "unverified"
                  ? "· нет согласованной оценки риска"
                  : "· предварительная оценка Backend, не юридический вывод"}
              </span>
            </p>
            <p className="observation">{event.description}</p>
            <div className="frame-grid">
              {event.evidence_frames.map((frame, i) => (
                <Frame key={i} frame={frame} index={i} demo={demo} />
              ))}
            </div>
            <div className="rule-source">
              <span>Нормативное основание</span>
              {sources.length ? (
                sources.map((source, sourceIndex) => (
                  <a
                    key={sourceIndex}
                    href={safeSourceUrl(source.url)!}
                    target="_blank"
                    rel="noopener noreferrer"
                    referrerPolicy="no-referrer"
                  >
                    {source.title}
                    {source.section ? ` · ${source.section}` : ""} ↗
                  </a>
                ))
              ) : (
                <p>
                  Источник не предоставлен. Нормативное основание требует
                  проверки.
                </p>
              )}
            </div>
          </article>
        );
      })}
      {!testMode && <HumanReview key={model.result.job_id} jobId={model.result.job_id} events={model.result.violations.length} />}
    </section>
  );
}
