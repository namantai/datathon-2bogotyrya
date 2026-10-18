import { parseAccepted, parseJob, UiError } from "./contracts";
import type { AnalysisService, DoneJob, Scenario } from "./contracts";

// Mock default; authenticated bridge publishes the actual server limit.
export const MAX_UPLOAD_BYTES = 128 * 1024 * 1024;
export const ANALYSIS_TIMEOUT_MS = 300_000;
// Compact event/timer label; retain the original fractional seconds for seeking.
export function formatClock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}
export function validateFile(file: File, limit = MAX_UPLOAD_BYTES): void {
  if (!/\.mov$/i.test(file.name))
    throw new UiError(
      "Выберите видео в формате .mov. Другие форматы не поддерживаются.",
    );
  if (!file.size) throw new UiError("Файл пуст. Выберите другую запись.");
  if (file.size > limit)
    throw new UiError(
      `Файл превышает лимит ${formatSize(limit)}. Выберите запись меньшего размера.`,
    );
}
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024)
    return `${(bytes / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} КиБ`;
  return `${(bytes / 1024 / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} МиБ`;
}
export function formatTime(seconds: number): string {
  const ms = Math.round(seconds * 1000),
    whole = Math.floor(ms / 1000),
    hours = Math.floor(whole / 3600);
  const fraction = String(ms % 1000)
    .padStart(3, "0")
    .replace(/0+$/, "");
  return `${hours ? `${String(hours).padStart(2, "0")}:` : ""}${String(Math.floor((whole % 3600) / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}${fraction ? `.${fraction}` : ""}`;
}
export function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const cancel = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, ms);
    signal.addEventListener("abort", cancel, { once: true });
  });
}
// Settles even when a future adapter fails to honor AbortSignal.
function abortable<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const cancel = () => reject(signal.reason);
    signal.addEventListener("abort", cancel, { once: true });
    promise
      .then(resolve, reject)
      .finally(() => signal.removeEventListener("abort", cancel));
  });
}
export async function analyze(
  api: AnalysisService,
  file: File,
  scenario: Scenario,
  signal: AbortSignal,
  onAccepted: (jobId: string) => void,
  options = { pollMs: 1500, timeoutMs: ANALYSIS_TIMEOUT_MS },
): Promise<DoneJob> {
  validateFile(file, api.maxUploadBytes);
  const controller = new AbortController();
  const deadline = performance.now() + options.timeoutMs;
  const timeoutError = () =>
    new UiError(
      "Время ожидания истекло (5 минут). Результат не получен; серверная обработка могла продолжиться.",
    );
  const cancel = () => controller.abort(signal.reason);
  signal.throwIfAborted();
  signal.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(
    () => controller.abort(timeoutError()),
    options.timeoutMs,
  );
  const active = controller.signal;
  const checkDeadline = () => {
    active.throwIfAborted();
    if (performance.now() >= deadline) throw timeoutError();
  };
  try {
    const jobId = parseAccepted(
      await abortable(api.submit(file, active, scenario), active),
    );
    checkDeadline();
    onAccepted(jobId);
    while (true) {
      await pause(options.pollMs, active);
      checkDeadline();
      const result = parseJob(
        await abortable(api.getJob(jobId, active), active),
        jobId,
      );
      checkDeadline();
      if (result.status === "failed")
        throw new UiError(
          result.failureReason === "timeout"
            ? "Превышен серверный лимит времени обработки. Результат не получен. Попробуйте другую запись."
            : "Не удалось завершить анализ. Попробуйте ещё раз или выберите другую запись.",
        );
      if (result.status === "done") return result;
    }
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", cancel);
    controller.abort();
  }
}
