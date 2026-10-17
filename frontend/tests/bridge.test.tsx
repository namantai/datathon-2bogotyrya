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
  getSession,
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
  expect(fetch.mock.lastCall?.[0]).toBe("/ui-api/jobs/a%2Fb");
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
  await expect(getSession(new AbortController().signal)).rejects.toThrow(
    "Нет связи",
  );
  fetch.mockResolvedValue(new Response("invalid-json"));
  await expect(getSession(new AbortController().signal)).rejects.toThrow(
    "некорректный ответ",
  );
  fetch.mockResolvedValue(response({ ...info, max_upload_bytes: -1 }));
  await expect(getSession(new AbortController().signal)).rejects.toThrow(
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

it("uses authenticated server upload limits", async () => {
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

it("shows login, wrong code, authenticated fake warning and logout", async () => {
  vi.stubEnv("VITE_UI_MODE", "bridge");
  const fetch = fetchMock().mockResolvedValueOnce(response({}, 401));
  render(<App />);
  const input = await screen.findByLabelText("Код доступа Web UI");
  await waitFor(() => expect((input as HTMLInputElement).disabled).toBe(false));
  fetch.mockResolvedValueOnce(response({}, 401));
  fireEvent.change(input, { target: { value: "wrong-code" } });
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect((input as HTMLInputElement).value).toBe("");
  fetch.mockResolvedValueOnce(response(info));
  fireEvent.change(input, { target: { value: "synthetic-ui-access-code" } });
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  expect(
    await screen.findByText(/Тестовый Backend · FakeAnalyzer/),
  ).toBeTruthy();
  expect(screen.queryByLabelText("Демонстрационный сценарий")).toBeNull();
  fetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
  fireEvent.click(screen.getByRole("button", { name: "Выйти" }));
  expect(await screen.findByLabelText("Код доступа Web UI")).toBeTruthy();
  expect(fetch.mock.lastCall?.[1]).toMatchObject({
    method: "DELETE",
    headers: { "X-CSRF-Token": info.csrf_token },
  });
});

it("returns to login when an upload sees an expired session", async () => {
  vi.stubEnv("VITE_UI_MODE", "bridge");
  const fetch = fetchMock().mockResolvedValueOnce(response(info));
  render(<App />);
  const input = await screen.findByLabelText("MOV-видео");
  fireEvent.change(input, { target: { files: [file()] } });
  fetch.mockResolvedValueOnce(response({}, 401));
  fireEvent.click(screen.getByRole("button", { name: "Начать проверку" }));
  expect(await screen.findByLabelText("Код доступа Web UI")).toBeTruthy();
  expect(screen.getByRole("alert").textContent).toContain("Сессия истекла");
});

it("restores the session without keeping credentials and ignores unmounted fetch", async () => {
  vi.stubEnv("VITE_UI_MODE", "bridge");
  let resolve!: (r: Response) => void;
  fetchMock().mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const view = render(<App />);
  await screen.findByLabelText("Код доступа Web UI");
  view.unmount();
  await act(async () => resolve(response(info)));
  expect(screen.queryByLabelText("MOV-видео")).toBeNull();
});

it("requires explicit dev mock and ignores mock selection in production", async () => {
  vi.stubEnv("DEV", true);
  vi.stubEnv("VITE_UI_MODE", "bridge");
  expect(await loadService()).toBeNull();
  vi.stubEnv("VITE_UI_MODE", "mock");
  expect((await loadService())?.mode).toBe("demo");
  vi.stubEnv("DEV", false);
  expect(await loadService()).toBeNull();
});
