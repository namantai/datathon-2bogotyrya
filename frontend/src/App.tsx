import { useEffect, useRef, useState } from "react";
import type { AnalysisService } from "./lib/contracts";
import { UiError } from "./lib/contracts";
import { loadService } from "./services";
import { bootstrapSession, createBridgeService } from "./services/bridge";
import { useAnalysis } from "./hooks/useAnalysis";
import { UploadCard } from "./components/UploadCard";
import { AnalysisStatus } from "./components/AnalysisStatus";
import { Results } from "./components/Results";
import { Icon } from "./components/Icon";

export default function App({ service }: { service?: AnalysisService | null }) {
  const [api, setApi] = useState<AnalysisService | null>(service ?? null);
  const [ready, setReady] = useState(service !== undefined);
  const [connectionError, setConnectionError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (service !== undefined) return;
    let mounted = true;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function connect() {
      setApi(null);
      setReady(false);
      setConnectionError("");
      try {
        const mock = await loadService();
        // StrictMode's discarded effect must not issue a second session POST.
        if (!mounted) return;
        if (mock) setApi(mock);
        else {
          controller = new AbortController();
          timer = setTimeout(() => controller?.abort(), 10_000);
          const info = await bootstrapSession(controller.signal);
          if (mounted && !controller.signal.aborted)
            setApi(
              createBridgeService(info, () => {
                if (mounted) void connect();
              }),
            );
        }
      } catch (error) {
        if (mounted)
          setConnectionError(
            error instanceof UiError
              ? error.message
              : "Backend не ответил. Повторите подключение.",
          );
      } finally {
        clearTimeout(timer);
        if (mounted) setReady(true);
      }
    }
    void connect();
    return () => {
      mounted = false;
      controller?.abort();
      clearTimeout(timer);
    };
  }, [service, attempt]);
  const analysis = useAnalysis(api);
  const demo = api?.mode === "demo";
  const testMode = demo || api?.analyzerMode === "fake";
  const unavailable = ready && !api;
  const error =
    connectionError ||
    analysis.error ||
    (unavailable
      ? "Анализ пока недоступен. Подключение к Backend не настроено."
      : "");
  const screen = error
    ? "error"
    : analysis.busy
      ? "analysis"
      : analysis.result
        ? "result"
        : "upload";
  const main = useRef<HTMLElement>(null);
  useEffect(() => {
    main.current?.querySelector<HTMLElement>("h1")?.focus();
  }, [screen]);
  function retry() {
    analysis.selectFile(null);
    if (connectionError || unavailable) setAttempt((value) => value + 1);
  }
  return (
    <div className="app-shell">
      <a href="#main" className="skip-link">
        К основному содержимому
      </a>
      <header className="header">
        <a className="brand" href="/" aria-label="ForteGuard AI — главная">
          <span className="bank-name">ForteBank</span>
          <span className="brand-divider" aria-hidden="true" />
          <span className="product-name">
            ForteGuard <em>AI</em>
          </span>
        </a>
        <span className="project-tag">DATATHON 2026</span>
      </header>
      <main
        id="main"
        ref={main}
        className={`main ${screen === "result" ? "main-results" : ""}`}
        data-screen={screen}
      >
        {(testMode || api?.analyzerMode === "disabled") && (
          <p className="mode-note" role="note">
            <Icon name="shield" size={17} />
            {demo
              ? "Демо · синтетические события и кадры. Видео не анализируется."
              : testMode
                ? "Тестовый режим · FakeAnalyzer. Это не AI-анализ. Только синтетические видео."
                : "ML-модель не подключена. Анализ завершится ошибкой, не вердиктом."}
          </p>
        )}
        {screen === "error" ? (
          <section className="panel error-card" aria-labelledby="error-title">
            <span className="state-icon">
              <Icon name="video" size={28} />
            </span>
            <h1 id="error-title" tabIndex={-1}>
              Не удалось выполнить проверку
            </h1>
            <p className="lead" role="alert">
              {error}
            </p>
            <button className="primary" onClick={retry} disabled={!ready}>
              {connectionError || unavailable
                ? "Повторить подключение"
                : "Попробовать снова"}
            </button>
            <p className="muted">
              Повторная загрузка начнётся только после вашего подтверждения.
            </p>
          </section>
        ) : screen === "analysis" ? (
          <AnalysisStatus
            startedAt={analysis.startedAt!}
            onCancel={analysis.cancel}
          />
        ) : screen === "result" && analysis.result ? (
          <Results
            model={{ result: analysis.result }}
            demo={demo}
            testMode={testMode}
            file={analysis.file}
            onReset={() => analysis.selectFile(null)}
          />
        ) : (
          <>
            {analysis.notice && (
              <p className="notice" role="status">
                {analysis.notice}
              </p>
            )}
            <UploadCard
              file={analysis.file}
              busy={analysis.busy}
              available={Boolean(api)}
              demo={demo}
              maxUploadBytes={api?.maxUploadBytes}
              onSelect={analysis.selectFile}
              onStart={analysis.start}
            />
            {!ready && (
              <p className="connection-note" role="status">
                Подключение к Backend…
              </p>
            )}
          </>
        )}
      </main>
      <footer className="footer">
        <span>Командный прототип · Не официальный сервис банка</span>
        <span>
          <Icon name="shield" size={14} /> Локальная обработка · Без
          идентификации лиц
        </span>
      </footer>
    </div>
  );
}
