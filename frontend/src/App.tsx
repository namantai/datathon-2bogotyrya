import { useEffect, useState } from "react";
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
  const analysis = useAnalysis(api),
    demo = api?.mode === "demo";
  return (
    <div className="app-shell">
      <a href="#main" className="skip-link">
        К основному содержимому
      </a>
      <header className="header">
        <a className="brand" href="/" aria-label="ForteGuard AI — главная">
          <span className="brand-mark">
            <Icon name="shield" size={26} />
          </span>
          <span>
            ForteGuard <em>AI</em>
          </span>
        </a>
        <div className="header-meta">
          <span className="project-tag">DATATHON 2026</span>
          <span className={`mode-tag ${demo ? "demo" : ""}`}>
            {demo
              ? "Демонстрационный режим"
              : api
                ? "Локальный Backend"
                : "Подключение не настроено"}
          </span>
        </div>
      </header>
      <main id="main" className="main">
        <div className="page-intro">
          <p className="eyebrow">ВИДЕОАНАЛИТИКА КАССОВЫХ ОПЕРАЦИЙ</p>
          <h1>
            Каждое событие —<br />
            <span>в поле внимания.</span>
          </h1>
          <p className="lead">
            Проверяйте записи, изучайте наблюдаемые действия
            <br className="desktop-break" /> и принимайте решения на основе
            кадров.
          </p>
        </div>
        {demo ? (
          <div className="demo-banner">
            <span className="demo-label">ДЕМО</span>
            <p>
              Вы проверяете интерфейс на синтетических данных. Видео не
              анализируется и не отправляется на сервер.
            </p>
          </div>
        ) : (
          <div className="demo-banner" role="status">
            <Icon name="shield" />
            <p>
              {api?.mode === "bridge"
                ? api.analyzerMode === "fake"
                  ? "Тестовый Backend · FakeAnalyzer. Видео загружается на сервер, но AI-анализ не выполняется. Вердикт заглушки не оценивает содержание видео. Используйте только синтетику."
                  : api.analyzerMode === "disabled"
                    ? "Локальная ML-модель не подключена. Задание завершится контролируемой ошибкой, а не вердиктом."
                    : "Видео обрабатывается локально. Результат требует проверки человеком."
                : ready
                  ? "Анализ пока недоступен. Ожидается безопасное серверное подключение; демонстрация отключена в production."
                  : "Подключение к Backend…"}
            </p>
          </div>
        )}
        {connectionError && (
          <div className="error-banner" role="alert">
            <p>{connectionError}</p>
            <button
              className="text-button"
              disabled={!ready}
              onClick={() => setAttempt((value) => value + 1)}
            >
              Повторить подключение
            </button>
          </div>
        )}
        {analysis.error && (
          <div className="error-banner" role="alert">
            <strong>Проверка не выполнена</strong>
            <p>{analysis.error}</p>
          </div>
        )}
        {analysis.notice && (
          <p className="notice" role="status">
            {analysis.notice}
          </p>
        )}
        <div className="workspace">
          <UploadCard
            file={analysis.file}
            busy={analysis.busy}
            available={Boolean(api)}
            demo={demo}
            maxUploadBytes={api?.maxUploadBytes}
            onSelect={analysis.selectFile}
            onStart={analysis.start}
            onCancel={analysis.cancel}
          />
          <AnalysisStatus
            phase={analysis.phase}
            jobId={analysis.jobId}
            demo={demo}
          />
        </div>
        {analysis.result ? (
          <Results
            model={{ result: analysis.result }}
            demo={demo}
            onReset={() => analysis.selectFile(null)}
          />
        ) : (
          <section className="empty-result">
            <span className="empty-icon">
              <Icon name="video" size={27} />
            </span>
            <div>
              <h2>Здесь появится результат</h2>
              <p>
                Вердикт, временные отметки и кадры по каждому обнаруженному
                событию.
              </p>
            </div>
          </section>
        )}
        <footer>
          <span>ForteGuard AI · Командный прототип</span>
          <span>Визуальный анализ · Без идентификации лиц</span>
        </footer>
      </main>
    </div>
  );
}
