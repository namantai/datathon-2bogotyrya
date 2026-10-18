import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, it, vi } from "vitest";
import App from "../src/App";
import { analyze } from "../src/lib/analysis";
import {
  createBridgeService,
  bootstrapSession,
  SessionExpired,
} from "../src/services/bridge";
import { loadService } from "../src/services";
import { done, file } from "./fixtures";

const info = {
  csrf_token: "synthetic-csrf-token-for-tests-only-123456",
  analyzer_mode: "fake" as const,
  max_upload_bytes: 1024,
};
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });
const fetchMock = () => vi.spyOn(globalThis, "fetch");

it("sends only same-origin multipart file, session cookie and CSRF, never a jury key", async () => {
  const fetch = fetchMock().mockResolvedValue(
    response({ job_id: "test-job" }, 202),
  );
  const service = createBridgeService(info, vi.fn());
  const signal = new AbortController().signal;
  await service.submit(file(), signal, "multiple");
  const [url, init] = fetch.mock.calls[0];
  expect(url).toBe("/ui-api/analyze");
  expect(init).toMatchObject({
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    signal,
  });
  expect(init?.headers).toEqual({ "X-CSRF-Token": info.csrf_token });
  expect([...(init?.body as FormData).keys()]).toEqual(["file"]);
  expect((init?.body as FormData).get("file")).toBeInstanceOf(File);
  fetch.mockResolvedValue(response(done()));
  await service.getJob("a/b", signal);
  expect(fetch.mock.lastCall?.[0]).toBe(
    "/ui-api/jobs/a%2Fb?include_details=true",
  );
});

it.each([400, 403, 404, 408, 413, 415, 422, 429, 500, 503])(
  "maps HTTP %s to a safe message",
  async (status) => {
    fetchMock().mockResolvedValue(
      response({ detail: "PRIVATE_STACK_AND_SECRET" }, status),
    );
    const promise = createBridgeService(info, vi.fn()).getJob(
      "id",
      new AbortController().signal,
    );
    await expect(promise).rejects.not.toThrow("PRIVATE_STACK_AND_SECRET");
  },
);

it("invalidates expired sessions and never treats network/JSON errors as a verdict", async () => {
  const expired = vi.fn();
  const fetch = fetchMock().mockResolvedValue(response({}, 401));
  const service = createBridgeService(info, expired);
  await expect(
    service.getJob("id", new AbortController().signal),
  ).rejects.toBeInstanceOf(SessionExpired);
  expect(expired).toHaveBeenCalledOnce();
  fetch.mockRejectedValue(new Error("PRIVATE_HOST"));
  await expect(bootstrapSession(new AbortController().signal)).rejects.toThrow(
    "Нет связи",
  );
  fetch.mockResolvedValue(new Response("invalid-json"));
  await expect(bootstrapSession(new AbortController().signal)).rejects.toThrow(
    "некорректный ответ",
  );
  fetch.mockResolvedValue(response({ ...info, max_upload_bytes: -1 }));
  await expect(bootstrapSession(new AbortController().signal)).rejects.toThrow(
    "конфигурация",
  );
});

it.each([0, 2])(
  "polls processing → done with %s server events and permits sequential uploads",
  async (count) => {
    const fetch = fetchMock();
    const service = createBridgeService(info, vi.fn());
    for (let i = 0; i < 2; i++) {
      fetch
        .mockResolvedValueOnce(response({ job_id: "test-job" }, 202))
        .mockResolvedValueOnce(response({ status: "processing" }))
        .mockResolvedValueOnce(response(done(count)));
      const result = await analyze(
        service,
        file(),
        "none",
        new AbortController().signal,
        vi.fn(),
        { pollMs: 1, timeoutMs: 1000 },
      );
      expect(result).toEqual(done(count));
    }
    expect(fetch).toHaveBeenCalledTimes(6);
  },
);

it("aborts real adapter polling and reports failed without fabricating results", async () => {
  const fetch = fetchMock()
    .mockResolvedValueOnce(response({ job_id: "test-job" }, 202))
    .mockResolvedValueOnce(response({ status: "failed" }));
  const service = createBridgeService(info, vi.fn());
  await expect(
    analyze(service, file(), "none", new AbortController().signal, vi.fn(), {
      pollMs: 1,
      timeoutMs: 1000,
    }),
  ).rejects.toThrow("Не удалось");
  fetch.mockResolvedValue(response({ job_id: "test-job" }, 202));
  const controller = new AbortController();
  await expect(
    analyze(
      service,
      file(),
      "none",
      controller.signal,
      () => controller.abort(),
      { pollMs: 1, timeoutMs: 1000 },
    ),
  ).rejects.toThrow();
  expect(fetch).toHaveBeenCalledTimes(3);
});

it("uses server upload limits", async () => {
  const fetch = fetchMock();
  await expect(
    analyze(
      createBridgeService({ ...info, max_upload_bytes: 1 }, vi.fn()),
      file(),
      "none",
      new AbortController().signal,
      vi.fn(),
    ),
  ).rejects.toThrow("лимит");
  expect(fetch).not.toHaveBeenCalled();
});

it("opens the dashboard immediately and bootstraps without credentials", async () => {
  vi.stubEnv("VITE_UI_MODE", "bridge");
  const fetch = fetchMock().mockResolvedValue(response(info));
  render(<App />);
  expect(screen.getByLabelText("MOV-видео")).toBeTruthy();
  expect(screen.queryByLabelText("Код доступа Web UI")).toBeNull();
  expect(screen.queryByRole("button", { name: /Войти|Выйти/ })).toBeNull();
  expect(await screen.findByText(/Тестовый режим · FakeAnalyzer/)).toBeTruthy();
  expect(screen.queryByLabelText("Демонстрационный сценарий")).toBeNull();
  expect(fetch).toHaveBeenCalledOnce();
  expect(fetch.mock.calls[0][0]).toBe("/ui-api/session");
  expect(fetch.mock.calls[0][1]).toMatchObject({
    method: "POST",
    credentials: "same-origin",
  });
  expect(fetch.mock.calls[0][1]?.body).toBeUndefined();
});

it.each([0, 2])(
  "uploads from the anonymous dashboard and shows %s events",
  async (count) => {
    vi.stubEnv("VITE_UI_MODE", "bridge");
    const fetch = fetchMock()
      .mockResolvedValueOnce(response(info))
      .mockResolvedValueOnce(response({ job_id: "test-job" }, 202))
      .mockResolvedValueOnce(response(done(count)));
    render(<App />);
    await screen.findByText(/Тестовый режим · FakeAnalyzer/);
    fireEvent.change(screen.getByLabelText("MOV-видео"), {
      target: { files: [file()] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
    await waitFor(
      () =>
        expect(
          screen.getByText(
            count
              ? "Тестовый результат: найдены события"
              : "Тестовый результат: событий нет",
          ),
        ).toBeTruthy(),
      { timeout: 3000 },
    );
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/ui-api/session",
      "/ui-api/analyze",
      "/ui-api/jobs/test-job?include_details=true",
    ]);
    expect(screen.queryByRole("button", { name: /Войти|Выйти/ })).toBeNull();
  },
);

it("restores an expired session once without automatically resubmitting the file", async () => {
  vi.stubEnv("VITE_UI_MODE", "bridge");
  const fetch = fetchMock()
    .mockResolvedValueOnce(response(info))
    .mockResolvedValueOnce(response({}, 401))
    .mockResolvedValueOnce(response(info));
  render(<App />);
  await screen.findByText(/Тестовый режим · FakeAnalyzer/);
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [file()] },
  });
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    expect.stringContaining("Сессия истекла"),
  );
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
  expect(fetch.mock.calls.map(([path]) => path)).toEqual([
    "/ui-api/session",
    "/ui-api/analyze",
    "/ui-api/session",
  ]);
  expect(screen.queryByLabelText("Код доступа Web UI")).toBeNull();
});

it("shows a separate error on bootstrap failure and retries only on explicit action", async () => {
  vi.stubEnv("VITE_UI_MODE", "bridge");
  const fetch = fetchMock().mockResolvedValueOnce(response({}, 503));
  render(<App />);
  const retry = await screen.findByRole("button", {
    name: "Повторить подключение",
  });
  expect(screen.queryByLabelText("MOV-видео")).toBeNull();
  expect(fetch).toHaveBeenCalledOnce();
  fetch.mockResolvedValueOnce(response(info));
  fireEvent.click(retry);
  await screen.findByText(/Тестовый режим · FakeAnalyzer/);
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("ignores an unmounted bootstrap response", async () => {
  vi.stubEnv("VITE_UI_MODE", "bridge");
  let resolve!: (r: Response) => void;
  const fetch = fetchMock().mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const view = render(<App />);
  await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
  const signal = fetch.mock.calls[0][1]?.signal;
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve(response(info)));
  expect(screen.queryByLabelText("MOV-видео")).toBeNull();
});

it("always selects the backend bridge, even with stale dev mock environment", async () => {
  vi.stubEnv("DEV", true);
  vi.stubEnv("VITE_UI_MODE", "bridge");
  expect(await loadService()).toBeNull();
  vi.stubEnv("VITE_UI_MODE", "mock");
  expect(await loadService()).toBeNull();
  vi.stubEnv("DEV", false);
  expect(await loadService()).toBeNull();
});
