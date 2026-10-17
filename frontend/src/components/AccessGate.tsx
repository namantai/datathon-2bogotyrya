import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { AnalysisService } from "../lib/contracts";
import { UiError } from "../lib/contracts";
import {
  createBridgeService,
  getSession,
  logoutSession,
  SessionExpired,
} from "../services/bridge";
import type { SessionInfo } from "../services/bridge";

export function AccessGate({
  children,
}: {
  children: (api: AnalysisService, logout: () => void) => ReactNode;
}) {
  const [connection, setConnection] = useState<{
    api: AnalysisService;
    info: SessionInfo;
  } | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const active = useRef<AbortController | null>(null);
  function connected(info: SessionInfo) {
    setConnection({
      info,
      api: createBridgeService(info, () => {
        setConnection(null);
        setError(
          "Сессия истекла. Войдите снова; серверное задание могло продолжиться.",
        );
      }),
    });
  }
  useEffect(() => {
    const controller = new AbortController();
    active.current = controller;
    const timer = setTimeout(() => controller.abort(), 10_000);
    void getSession(controller.signal)
      .then((info) => {
        if (active.current === controller && !controller.signal.aborted)
          connected(info);
      })
      .catch((e: unknown) => {
        if (active.current === controller && !(e instanceof SessionExpired))
          setError(
            e instanceof UiError
              ? e.message
              : "Backend не ответил. Повторите вход.",
          );
      })
      .finally(() => {
        clearTimeout(timer);
        if (active.current === controller) {
          active.current = null;
          setBusy(false);
        }
      });
    return () => {
      active.current?.abort();
      active.current = null;
      clearTimeout(timer);
    };
  }, []);
  async function authenticate(logout = false) {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    const timer = setTimeout(() => controller.abort(), 10_000);
    setBusy(true);
    setError("");
    const submittedCode = code;
    setCode("");
    try {
      if (logout && connection) {
        await logoutSession(connection.info, controller.signal);
        setConnection(null);
      } else connected(await getSession(controller.signal, submittedCode));
    } catch (e) {
      if (logout && e instanceof SessionExpired) setConnection(null);
      setError(
        e instanceof UiError
          ? e.message
          : "Запрос не завершён. Проверьте Backend и повторите вход.",
      );
    } finally {
      clearTimeout(timer);
      if (active.current === controller) {
        active.current = null;
        setBusy(false);
      }
    }
  }
  if (connection)
    return (
      <>
        {error && (
          <p className="error-banner" role="alert">
            {error}
          </p>
        )}
        {children(connection.api, () => {
          void authenticate(true);
        })}
      </>
    );
  return (
    <main className="access-page">
      <form
        className="panel access-panel"
        onSubmit={(event) => {
          event.preventDefault();
          void authenticate();
        }}
      >
        <p className="eyebrow">FORTEGUARD · ЛОКАЛЬНЫЙ СЕРВИС</p>
        <h1>Доступ к проверке видео</h1>
        <p>
          Введите отдельный код доступа Web UI. Ключ API жюри здесь не
          используется.
        </p>
        <label htmlFor="access-code">Код доступа Web UI</label>
        <input
          id="access-code"
          type="password"
          autoComplete="off"
          maxLength={256}
          required
          disabled={busy}
          value={code}
          onChange={(event) => setCode(event.target.value)}
        />
        <button className="primary" disabled={busy} type="submit">
          {busy ? "Проверка доступа…" : "Войти"}
        </button>
        {error && <p role="alert">{error}</p>}
        <small>
          Короткая серверная сессия. Видео не передаётся облачным AI-сервисам.
        </small>
      </form>
    </main>
  );
}
