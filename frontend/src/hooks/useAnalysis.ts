import { useEffect, useRef, useState } from "react";
import { analyze, validateFile } from "../lib/analysis";
import { UiError } from "../lib/contracts";
import type { AnalysisService, DoneJob, Scenario } from "../lib/contracts";
export type Phase =
  "idle" | "file_selected" | "uploading" | "processing" | "done" | "failed";
export function useAnalysis(api: AnalysisService | null) {
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<DoneJob | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => {
    const leavePage = () => {
      if (!active.current) return;
      active.current?.abort();
      active.current = null;
      setPhase("file_selected");
      setNotice(
        "Ожидание остановлено при уходе со страницы. Серверное задание могло продолжиться.",
      );
    };
    window.addEventListener("pagehide", leavePage);
    return () => {
      window.removeEventListener("pagehide", leavePage);
      active.current?.abort();
      active.current = null;
    };
  }, []);
  function selectFile(next: File | null, multiple = false) {
    if (active.current) return;
    setResult(null);
    setJobId("");
    setNotice("");
    setError("");
    setStartedAt(null);
    try {
      if (multiple) throw new UiError("Выберите одну видеозапись за раз.");
      if (next) validateFile(next, api?.maxUploadBytes);
      setFile(next);
      setPhase(next ? "file_selected" : "idle");
    } catch (e) {
      setFile(null);
      setPhase("failed");
      setError((e as UiError).message);
    }
  }
  async function start(scenario: Scenario) {
    if (active.current) return;
    if (!api) {
      setError("Подключение к сервису пока недоступно.");
      return;
    }
    if (!file) {
      setError("Сначала выберите MOV-видео.");
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    setStartedAt(performance.now());
    setPhase("uploading");
    setError("");
    setNotice("");
    setResult(null);
    setJobId("");
    try {
      const completed = await analyze(
        api,
        file,
        scenario,
        controller.signal,
        (id) => {
          setJobId(id);
          setPhase("processing");
        },
      );
      if (active.current === controller) {
        setResult(completed);
        setPhase("done");
      }
    } catch (e) {
      if (active.current === controller) {
        setPhase("failed");
        setError(
          e instanceof UiError
            ? e.message
            : "Ошибка связи или загрузки. Анализ не завершён. Попробуйте ещё раз.",
        );
      }
    } finally {
      if (active.current === controller) active.current = null;
    }
  }
  function cancel() {
    active.current?.abort();
    active.current = null;
    setPhase(file ? "file_selected" : "idle");
    setNotice(
      "Ожидание отменено. Это не отменяет уже созданное задание на сервере.",
    );
  }
  return {
    file,
    phase,
    error,
    notice,
    jobId,
    result,
    startedAt,
    selectFile,
    start,
    cancel,
    busy: phase === "uploading" || phase === "processing",
  };
}
