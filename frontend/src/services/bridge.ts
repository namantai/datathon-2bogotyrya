import { UiError } from "../lib/contracts";
import type { AnalysisService } from "../lib/contracts";

export class SessionExpired extends UiError {}
export interface SessionInfo {
  csrf_token: string;
  analyzer_mode: "fake" | "disabled" | "local";
  max_upload_bytes: number;
}

const messages: Record<number, string> = {
  400: "Файл или запрос некорректен. Выберите исправный MOV.",
  403: "Проверка безопасности не пройдена. Обновите страницу.",
  404: "Задание не найдено или недоступно этой сессии.",
  408: "Время загрузки истекло. Попробуйте ещё раз.",
  413: "Файл превышает допустимый размер.",
  415: "Неподдерживаемый формат. Выберите MOV.",
  422: "Выберите один непустой MOV-файл.",
  429: "Лимит запросов достигнут. Дождитесь завершения задания и повторите через минуту.",
  503: "Сервис временно недоступен или не настроен. Обратитесь к оператору.",
};

async function http(path: string, init: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`/ui-api${path}`, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
  } catch {
    if (init.signal?.aborted) throw init.signal.reason;
    throw new UiError(
      "Нет связи с Backend. Проверьте подключение и повторите попытку.",
    );
  }
  if (response.status === 401)
    throw new SessionExpired(
      "Сессия истекла. После восстановления подключения можно запустить новую проверку. Файл автоматически не отправляется повторно; прежнее серверное задание могло продолжиться.",
    );
  if (!response.ok)
    throw new UiError(
      messages[response.status] ??
        "Сервер не смог выполнить запрос. Попробуйте позже.",
    );
  if (response.status === 204) return null;
  try {
    return await response.json();
  } catch {
    throw new UiError("Сервис вернул некорректный ответ.");
  }
}

export async function bootstrapSession(
  signal: AbortSignal,
): Promise<SessionInfo> {
  const value = await http("/session", { method: "POST", signal });
  const v = value as Partial<SessionInfo> | null;
  if (
    !v ||
    typeof v.csrf_token !== "string" ||
    v.csrf_token.length < 32 ||
    !["fake", "disabled", "local"].includes(v.analyzer_mode ?? "") ||
    !Number.isSafeInteger(v.max_upload_bytes) ||
    (v.max_upload_bytes ?? 0) <= 0
  )
    throw new UiError("Некорректная конфигурация серверной сессии.");
  return v as SessionInfo;
}

export function createBridgeService(
  info: SessionInfo,
  onExpired: () => void,
): AnalysisService {
  let expired = false;
  async function call(path: string, init: RequestInit) {
    try {
      return await http(path, init);
    } catch (error) {
      if (error instanceof SessionExpired && !expired) {
        expired = true;
        onExpired();
      }
      throw error;
    }
  }
  return {
    mode: "bridge",
    analyzerMode: info.analyzer_mode,
    maxUploadBytes: info.max_upload_bytes,
    submit(file, signal) {
      const body = new FormData();
      body.append("file", file);
      return call("/analyze", {
        method: "POST",
        body,
        signal,
        headers: { "X-CSRF-Token": info.csrf_token },
      });
    },
    getJob(jobId, signal) {
      return call(`/jobs/${encodeURIComponent(jobId)}`, { signal });
    },
  };
}
