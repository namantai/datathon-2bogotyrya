const foundationItems = [
  "Локальный Backend на FastAPI",
  "Интерфейс на React и TypeScript",
  "Подготовка к интеграции локального ML",
];

function App() {
  return (
    <main className="page-shell">
      <section className="hero" aria-labelledby="page-title">
        <p className="eyebrow">ForteBank DATATHON 2026</p>
        <h1 id="page-title">Анализ кассовых операций по видеозаписи</h1>
        <p className="lead">
          Создаём локальный сервис, который поможет находить визуальные признаки
          возможных нарушений и показывать проверяемые доказательства.
        </p>

        <div className="status-card" role="status">
          <span className="status-dot" aria-hidden="true" />
          <div>
            <strong>Этап 01: технический фундамент</strong>
            <p>
              Загрузка и анализ видео ещё не реализованы. Сейчас доступен только
              стартовый интерфейс проекта.
            </p>
          </div>
        </div>

        <ul className="foundation-list" aria-label="Компоненты фундамента">
          {foundationItems.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>
    </main>
  );
}

export default App;
