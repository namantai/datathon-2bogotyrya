import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import App from "../src/App";
import { Results } from "../src/components/Results";
import { createMockService } from "../src/services/mock";
import { loadService } from "../src/services";
import type { AnalysisService } from "../src/lib/contracts";
import { done, file, JPEG } from "./fixtures";

function api(): AnalysisService {
  return {
    mode: "demo",
    submit: vi.fn().mockResolvedValue({ job_id: "test-job" }),
    getJob: vi.fn().mockResolvedValue(done()),
  };
}
function select() {
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [file()] },
  });
}
async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

it("renders only upload, disables start without a valid file and accepts keyboard upload", async () => {
  render(<App service={api()} />);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toContain(
    "Загрузите видеозапись",
  );
  const user = userEvent.setup();
  expect(
    (screen.getByRole("button", { name: "Начать анализ" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  await user.click(screen.getByRole("button", { name: "Начать анализ" }));
  expect(screen.queryByRole("alert")).toBeNull();
  await user.upload(screen.getByLabelText("MOV-видео"), file());
  expect(screen.getByText("sample.MOV")).toBeTruthy();
});
it("rejects invalid drop, multiple files and empty file", () => {
  const { container } = render(<App service={api()} />);
  const zone = () => container.querySelector(".dropzone")!;
  fireEvent.drop(zone(), {
    dataTransfer: {
      files: [new File(["x"], "bad.exe", { type: "video/quicktime" })],
    },
  });
  expect(screen.getByRole("alert").textContent).toContain("формате .mov");
  fireEvent.click(screen.getByRole("button", { name: "Попробовать снова" }));
  fireEvent.drop(zone(), { dataTransfer: { files: [file(), file()] } });
  expect(screen.getByRole("alert").textContent).toContain("одну");
  fireEvent.click(screen.getByRole("button", { name: "Попробовать снова" }));
  fireEvent.drop(zone(), {
    dataTransfer: { files: [new File([], "empty.mov")] },
  });
  expect(screen.getByRole("alert").textContent).toContain("пуст");
});
it("shows one analysis screen across upload/polling and stops duplicate submission", async () => {
  vi.useFakeTimers();
  const service = api();
  let accept!: (value: unknown) => void;
  vi.mocked(service.submit).mockImplementation(
    () =>
      new Promise((resolve) => {
        accept = resolve;
      }),
  );
  render(<App service={service} />);
  select();
  const button = screen.getByRole("button", { name: "Начать анализ" });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(service.submit).toHaveBeenCalledTimes(1);
  expect(
    screen.getByRole("heading", { name: "Идёт анализ видео..." }),
  ).toBeTruthy();
  expect(screen.queryByLabelText("MOV-видео")).toBeNull();
  expect(screen.getAllByRole("progressbar")).toHaveLength(1);
  await act(async () => {
    accept({ job_id: "test-job" });
  });
  expect(
    screen.getByRole("heading", { name: "Идёт анализ видео..." }),
  ).toBeTruthy();
  expect(screen.getByRole("timer").textContent).toBe("00:00 / 05:00");
  await advance(1500);
  expect(
    screen.getByRole("heading", { name: "Тестовый результат: событий нет" }),
  ).toBeTruthy();
  await advance(10_000);
  expect(service.getJob).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("progressbar")).toBeNull();
  expect(screen.queryByLabelText("MOV-видео")).toBeNull();
  fireEvent.click(
    screen.getByRole("button", { name: "Проверить другое видео" }),
  );
  expect(screen.queryByText("sample.MOV")).toBeNull();
});
it.each(["none", "one", "multiple", "error"])(
  "runs actual mock scenario %s",
  async (scenario) => {
    vi.useFakeTimers();
    const context = {
      fillRect: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      fillText: vi.fn(),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
      context as unknown as CanvasRenderingContext2D,
    );
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(
      `data:image/jpeg;base64,${JPEG}`,
    );
    render(<App service={createMockService()} />);
    select();
    fireEvent.change(screen.getByLabelText("Демонстрационный сценарий"), {
      target: { value: scenario },
    });
    fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
    await advance(700);
    await advance(3000);
    if (scenario === "error")
      expect(screen.getByRole("alert").textContent).toContain(
        "Не удалось завершить",
      );
    else if (scenario === "none")
      expect(
        screen.getByRole("heading", {
          name: "Тестовый результат: событий нет",
        }),
      ).toBeTruthy();
    else {
      expect(screen.getAllByRole("article")).toHaveLength(
        scenario === "one" ? 1 : 2,
      );
      expect(screen.getAllByRole("img")).toHaveLength(
        scenario === "one" ? 3 : 4,
      );
      expect(screen.getAllByRole("img")[0].getAttribute("src")).toBe(
        `data:image/jpeg;base64,${JPEG}`,
      );
      expect(screen.getByText("00:12", { selector: ".timecode" })).toBeTruthy();
    }
    // jsdom queues a zero-delay selectionchange event when the new heading receives focus.
    await advance(0);
    expect(vi.getTimerCount()).toBe(0);
  },
);
it.each([new Error("secret internal path"), new SyntaxError("bad JSON")])(
  "sanitizes network, upload and JSON errors",
  async (error) => {
    const service = api();
    vi.mocked(service.submit).mockRejectedValue(error);
    render(<App service={service} />);
    select();
    fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Ошибка связи");
    expect(alert.textContent).not.toContain(error.message);
  },
);
it("cancels and unmounts without further polling", async () => {
  vi.useFakeTimers();
  const service = api();
  const view = render(<App service={service} />);
  select();
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  await advance(0);
  fireEvent.click(screen.getByRole("button", { name: "Отменить ожидание" }));
  await advance(5000);
  expect(service.getJob).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  await advance(0);
  view.unmount();
  await advance(5000);
  expect(service.getJob).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});
it("disables mock in production", async () => {
  vi.stubEnv("DEV", false);
  expect(() => createMockService()).toThrow();
  expect(await loadService()).toBeNull();
  render(<App service={null} />);
  expect(screen.queryByRole("button", { name: "Начать анализ" })).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("недоступен");
  expect(screen.queryByLabelText("Демонстрационный сценарий")).toBeNull();
});
it("stops polling on pagehide and ignores late upload completion", async () => {
  vi.useFakeTimers();
  const service = api();
  let accept!: (value: unknown) => void;
  vi.mocked(service.submit).mockImplementation(
    () =>
      new Promise((resolve) => {
        accept = resolve;
      }),
  );
  render(<App service={service} />);
  select();
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  fireEvent(window, new Event("pagehide"));
  await act(async () => {
    accept({ job_id: "test-job" });
  });
  await advance(10_000);
  expect(service.getJob).not.toHaveBeenCalled();
  expect(screen.queryByText("Обработка записи…")).toBeNull();
  expect(vi.getTimerCount()).toBe(0);
});
it("fails safely when submit omits job_id", async () => {
  const service = api();
  vi.mocked(service.submit).mockResolvedValue({});
  render(<App service={service} />);
  select();
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  expect((await screen.findByRole("alert")).textContent).toContain(
    "некорректный ответ",
  );
  expect(service.getJob).not.toHaveBeenCalled();
});
it.each(["not-base64", JPEG])(
  "shows broken JPEG fallback and escapes untrusted text",
  (value) => {
    const result = done(1);
    result.violations[0].type = "<img src=x onerror=alert(1)>";
    result.violations[0].evidence_frames[0].image_base64 = value;
    render(<Results model={{ result }} demo={false} onReset={() => {}} />);
    if (value === JPEG) fireEvent.error(screen.getByRole("img"));
    expect(screen.getByRole("alert").textContent).toContain("повреждён");
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText(result.violations[0].type)).toBeTruthy();
  },
);
