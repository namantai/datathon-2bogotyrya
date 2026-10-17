import { useState } from "react";
import { jpegSource, safeSourceUrl } from "../lib/contracts";
import type { EvidenceFrame, ResultViewModel } from "../lib/contracts";
import { formatTime } from "../lib/analysis";
import { Icon } from "./Icon";
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
  onReset,
}: {
  model: ResultViewModel;
  demo: boolean;
  onReset: () => void;
}) {
  const positive = model.result.verdict === "violation";
  return (
    <section className="results" aria-labelledby="results-title">
      <div className="results-top">
        <p className="eyebrow">РЕЗУЛЬТАТ ПРОВЕРКИ</p>
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
          <h2 id="results-title">
            {positive
              ? "Обнаружены признаки возможных нарушений"
              : "Нарушение не обнаружено"}
          </h2>
          <p>
            {positive
              ? `Событий для просмотра: ${model.result.violations.length}. Изучите описание и кадры каждого события.`
              : "Список обнаруженных событий пуст."}
          </p>
        </div>
      </div>
      <p className="result-note">
        Результат помогает проверяющему и не является окончательным юридическим
        выводом.
      </p>
      {model.result.violations.map((event, index) => {
        const source = model.sources?.[index],
          sourceUrl = source && safeSourceUrl(source.url);
        return (
          <article
            className="panel event-card"
            key={`${model.result.job_id}-${index}`}
          >
            <div className="event-heading">
              <span className="event-index">
                {String(index + 1).padStart(2, "0")}
              </span>
              <h3>{event.type}</h3>
              <span className="timecode">
                <Icon name="clock" size={15} />
                {formatTime(event.timecode_sec)}
              </span>
            </div>
            <p className="observation">{event.description}</p>
            <div className="frame-grid">
              {event.evidence_frames.map((frame, i) => (
                <Frame key={i} frame={frame} index={i} demo={demo} />
              ))}
            </div>
            <div className="rule-source">
              <span>Нормативное основание</span>
              {source && sourceUrl ? (
                <a
                  href={sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  referrerPolicy="no-referrer"
                >
                  {source.title}
                  {source.section ? ` · ${source.section}` : ""} ↗
                </a>
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
    </section>
  );
}
