import { StrictMode } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import App from "../src/App";
import { Results } from "../src/components/Results";
import { formatClock } from "../src/lib/analysis";
import type { AnalysisService } from "../src/lib/contracts";
import { createBridgeService } from "../src/services/bridge";
import { done, file } from "./fixtures";

const service = (): AnalysisService => ({
  mode: "bridge",
  analyzerMode: "local",
  maxUploadBytes: 1024,
  submit: vi.fn().mockResolvedValue({ job_id: "test-job" }),
  getJob: vi.fn().mockResolvedValue(done()),
});
const start = () => {
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [file()] },
  });
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
};
async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

it.each([0, 1, 2])(
  "has mutually exclusive screens and a real successful verdict for %s events",
  async (count) => {
    vi.useFakeTimers();
    const api = service();
    vi.mocked(api.getJob).mockResolvedValue(done(count));
    const { container } = render(<App service={api} />);
    expect(container.querySelector("main")?.dataset.screen).toBe("upload");
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    expect(container.querySelector("input")?.multiple).toBe(false);
    expect(container.querySelector("nav, aside")).toBeNull();
    expect(
      screen.queryByText(/История проверок|Настройки|Статистика/),
    ).toBeNull();
    expect(screen.getAllByText("ForteBank")).toHaveLength(1);
    start();
    expect(container.querySelector("main")?.dataset.screen).toBe("analysis");
    expect(screen.queryByLabelText("MOV-видео")).toBeNull();
    expect(screen.queryByRole("article")).toBeNull();
    await tick(1500);
    expect(container.querySelector("main")?.dataset.screen).toBe("result");
    expect(
      screen.getByRole("heading", { name: "Анализ завершён" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", {
        name: count
          ? "Обнаружены признаки возможных нарушений"
          : "Нарушения не обнаружены",
      }),
    ).toBeTruthy();
    expect(screen.queryAllByRole("article")).toHaveLength(count);
    expect(screen.queryAllByText(/Критичность: не определена/)).toHaveLength(
      count,
    );
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("timer")).toBeNull();
    expect(screen.queryByLabelText("MOV-видео")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Проверить другое видео" }),
    );
    expect(container.querySelector("video")).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:synthetic");
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    await tick(0); // Flush focus/selectionchange tasks, not application timers.
    expect(vi.getTimerCount()).toBe(0);
    expect(
      (
        screen.getByRole("button", {
          name: "Начать анализ",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  },
);

it("one timer covers upload AND processing; timeout removes progress and never shows a verdict", async () => {
  vi.useFakeTimers();
  const api = service();
  let accept!: (v: unknown) => void;
  vi.mocked(api.submit).mockImplementation(
    () =>
      new Promise((resolve) => {
        accept = resolve;
      }),
  );
  vi.mocked(api.getJob).mockImplementation(() => new Promise(() => {}));
  render(<App service={api} />);
  start();
  await tick(152_000);
  expect(screen.getAllByRole("timer")).toHaveLength(1);
  expect(screen.getByRole("timer").textContent).toBe("02:32 / 05:00");
  expect((screen.getByRole("progressbar") as HTMLProgressElement).value).toBe(
    152_000,
  );
  await act(async () => accept({ job_id: "test-job" }));
  await tick(1000);
  expect(screen.getByRole("timer").textContent).toBe("02:33 / 05:00");
  await tick(147_000);
  expect(screen.getByRole("alert").textContent).toContain(
    "Время ожидания истекло",
  );
  expect(screen.queryByRole("progressbar")).toBeNull();
  expect(screen.queryByText("Анализ завершён")).toBeNull();
  await tick(0);
  expect(vi.getTimerCount()).toBe(0);
  fireEvent.click(screen.getByRole("button", { name: "Попробовать снова" }));
  expect(screen.queryByText("sample.MOV")).toBeNull();
  expect(api.submit).toHaveBeenCalledOnce();
});

it("uses server upload size and clears invalid selections before retry", () => {
  const api = service();
  render(<App service={api} />);
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [new File([new Uint8Array(1025)], "large.mov")] },
  });
  expect(screen.getByRole("alert").textContent).toContain("превышает лимит");
  expect(screen.queryByLabelText("MOV-видео")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Попробовать снова" }));
  expect(screen.queryByText("large.mov")).toBeNull();
  expect(api.submit).not.toHaveBeenCalled();
});

it.each([401, 403, 413, 415, 429, 500, 503])(
  "shows isolated error screen for Backend HTTP %s without auto reupload",
  async (status) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("{}", { status }),
    );
    const api = createBridgeService(
      {
        csrf_token: "synthetic-token-long-enough-for-tests",
        analyzer_mode: "local",
        max_upload_bytes: 1024,
      },
      vi.fn(),
    );
    render(<App service={api} />);
    start();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByLabelText("MOV-видео")).toBeNull();
    expect(screen.queryByText("Анализ завершён")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Попробовать снова" }));
    expect(fetch).toHaveBeenCalledOnce();
  },
);

it("seeks exact fractional event time but renders a single MM:SS, and frees every StrictMode blob", () => {
  const result = done(1);
  result.violations[0].timecode_sec = 152.42;
  const { container, unmount } = render(
    <StrictMode>
      <Results
        model={{ result }}
        demo={false}
        file={file()}
        onReset={() => {}}
      />
    </StrictMode>,
  );
  const player = container.querySelector("video")!;
  const button = screen.getByRole("button", {
    name: "Перейти к событию 1, 02:32",
  });
  fireEvent.click(button); // Queue seeking until metadata is ready.
  expect(player.currentTime).toBe(0);
  Object.defineProperty(player, "readyState", { configurable: true, value: 1 });
  Object.defineProperty(player, "duration", { configurable: true, value: 180 });
  fireEvent.loadedMetadata(player);
  expect(player.currentTime).toBe(152.42);
  expect(button.textContent).toBe("02:32");
  unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledTimes(
    vi.mocked(URL.createObjectURL).mock.calls.length,
  );
  expect(player.getAttribute("src")).toBeNull();
});

it("keeps evidence available when a browser cannot play MOV", () => {
  const { container } = render(
    <Results
      model={{ result: done(1) }}
      demo={false}
      file={file()}
      onReset={() => {}}
    />,
  );
  fireEvent.error(container.querySelector("video")!);
  expect(screen.getByText(/Браузер не поддерживает/)).toBeTruthy();
  expect(screen.getByRole("img")).toBeTruthy();
  expect(
    (
      screen.getByRole("button", {
        name: /Перейти к событию/,
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});

it("does not present fake no_violation as a real successful AI verdict", async () => {
  vi.useFakeTimers();
  const api = { ...service(), analyzerMode: "fake" as const };
  render(<App service={api} />);
  start();
  await tick(1500);
  expect(screen.getByText(/Тестовый режим · FakeAnalyzer/)).toBeTruthy();
  expect(screen.queryByText("Нарушения не обнаружены")).toBeNull();
  expect(screen.getByText("Тестовый результат: событий нет")).toBeTruthy();
});

it("formats event labels without ranges, fractions or rounding into the next second", () => {
  expect(formatClock(152.42)).toBe("02:32");
  expect(formatClock(59.999)).toBe("00:59");
  expect(formatClock(3600)).toBe("60:00");
});
