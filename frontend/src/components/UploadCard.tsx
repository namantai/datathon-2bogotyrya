import { useRef, useState } from "react";
import type { Scenario } from "../lib/contracts";
import { formatSize, MAX_UPLOAD_BYTES } from "../lib/analysis";
import { Icon } from "./Icon";
interface Props {
  file: File | null;
  busy: boolean;
  available: boolean;
  demo: boolean;
  maxUploadBytes?: number;
  onSelect: (file: File | null, multiple?: boolean) => void;
  onStart: (scenario: Scenario) => void;
  onCancel: () => void;
}
export function UploadCard({
  file,
  busy,
  available,
  demo,
  maxUploadBytes = MAX_UPLOAD_BYTES,
  onSelect,
  onStart,
  onCancel,
}: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [scenario, setScenario] = useState<Scenario>("multiple");
  function choose(files: FileList | null) {
    if (files?.length) onSelect(files[0], files.length > 1);
  }
  return (
    <section className="panel upload-card" aria-labelledby="upload-title">
      <div className="section-heading">
        <span className="section-number">01</span>
        <h2 id="upload-title">Видеозапись</h2>
        <span className="format-tag">MOV</span>
      </div>
      <p className="muted">
        Одна запись — одна проверка. Выберите файл с вашего устройства.
      </p>
      <div
        className={`dropzone ${dragging ? "dragging" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy && available) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!busy && available) choose(event.dataTransfer.files);
        }}
      >
        <span className="upload-icon">
          <Icon name="upload" size={28} />
        </span>
        <h3>Перетащите видео сюда</h3>
        <p>или выберите файл на устройстве</p>
        <button
          type="button"
          className="secondary"
          disabled={busy || !available}
          onClick={() => input.current?.click()}
        >
          {file ? "Заменить файл" : "Выбрать файл"}
        </button>
        <input
          ref={input}
          className="sr-only"
          tabIndex={-1}
          type="file"
          aria-label="MOV-видео"
          accept=".mov"
          disabled={busy || !available}
          onChange={(event) => {
            choose(event.target.files);
            event.target.value = "";
          }}
        />
        <small>.mov · до {formatSize(maxUploadBytes)}</small>
      </div>
      {file && (
        <div className="selected-file">
          <span className="file-icon">
            <Icon name="video" />
          </span>
          <div>
            <strong>{file.name}</strong>
            <small>{formatSize(file.size)} · MOV</small>
          </div>
          <button
            className="text-button"
            disabled={busy}
            onClick={() => onSelect(null)}
            aria-label="Удалить выбранный файл"
          >
            Убрать
          </button>
        </div>
      )}
      {demo && (
        <div className="demo-selector">
          <label htmlFor="scenario">Демонстрационный сценарий</label>
          <select
            id="scenario"
            disabled={busy}
            value={scenario}
            onChange={(event) => setScenario(event.target.value as Scenario)}
          >
            <option value="multiple">Несколько тестовых событий</option>
            <option value="one">Одно тестовое событие</option>
            <option value="none">Без нарушений</option>
            <option value="error">Ошибка обработки</option>
          </select>
          <small>Результат задан сценарием и не зависит от видео.</small>
        </div>
      )}
      <div className="upload-actions">
        <button
          className="primary"
          disabled={busy || !available}
          onClick={() => onStart(scenario)}
        >
          {busy ? "Проверка выполняется" : "Начать проверку"}
          <Icon name="arrow" size={18} />
        </button>
        {busy && (
          <button className="text-button" onClick={onCancel}>
            Отменить ожидание
          </button>
        )}
      </div>
      <p className="privacy-note">
        <Icon name="shield" size={16} />
        {demo
          ? "В деморежиме файл остаётся в браузере."
          : "Видео отправляется только на локальный Backend."}
      </p>
    </section>
  );
}
