import { StrictMode } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import App from "../src/App";
import { file } from "./fixtures";

const session = {
  csrf_token: "synthetic-session-lifecycle-csrf-token-123456",
  analyzer_mode: "fake",
  max_upload_bytes: 1024,
};

function server(bootstrapStatus = 200) {
  vi.useFakeTimers();
  vi.stubEnv("VITE_UI_MODE", "bridge");
  let valid = true;
  const requests: { path: string; method: string }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    requests.push({ path, method });
    // Delayed replies make a render/retry loop observable without hanging act().
    await new Promise((resolve) => setTimeout(resolve, 1));
    if (path === "/ui-api/session" && method === "POST") {
      valid = bootstrapStatus === 200;
      return new Response(JSON.stringify(valid ? session : {}), {
        status: bootstrapStatus,
      });
    }
    return new Response(JSON.stringify({}), { status: valid ? 500 : 401 });
  });
  return {
    requests,
    posts: () =>
      requests.filter(
        (r) => r.path === "/ui-api/session" && r.method === "POST",
      ).length,
    gets: () =>
      requests.filter((r) => r.path === "/ui-api/session" && r.method === "GET")
        .length,
    expire: () => {
      valid = false;
    },
  };
}

async function tick(ms = 10) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

it.each(
  [false, true].flatMap((strict) =>
    [200, 401, 429, 503].map((status) => ({ strict, status })),
  ),
)(
  "one bootstrap, zero session GETs across rerenders/idle (StrictMode=$strict, status=$status)",
  async ({ strict, status }) => {
    const api = server(status);
    const tree = () =>
      strict ? (
        <StrictMode>
          <App />
        </StrictMode>
      ) : (
        <App />
      );
    const view = render(tree());
    expect(screen.getByLabelText("MOV-видео")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Войти|Выйти/ })).toBeNull();
    await tick();
    for (let i = 0; i < 20; i++) {
      view.rerender(tree());
      if (status === 200) {
        fireEvent.change(screen.getByLabelText("MOV-видео"), {
          target: { files: [file()] },
        });
      } else expect(screen.getByRole("alert")).toBeTruthy();
      await tick();
      expect(api.posts()).toBe(1);
    }
    for (let i = 0; i < 60; i++) {
      await tick(1000);
      expect(api.posts()).toBe(1);
      expect(api.gets()).toBe(0);
    }
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  },
);

it("reopens and renews after expiry without polling sessions or replaying uploads", async () => {
  const api = server();
  const tree = (
    <StrictMode>
      <App />
    </StrictMode>
  );
  let view = render(tree);
  await tick();
  expect(api.posts()).toBe(1);
  view.unmount();
  view = render(tree);
  await tick();
  expect(api.posts()).toBe(2);
  api.expire();
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [file()] },
  });
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  await tick();
  expect(screen.getByRole("alert").textContent).toContain("Сессия истекла");
  for (let i = 0; i < 60; i++) {
    await tick(1000);
    expect(api.posts()).toBe(3);
    expect(api.gets()).toBe(0);
    expect(
      api.requests.filter((r) => r.path === "/ui-api/analyze"),
    ).toHaveLength(1);
  }
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});

it("stops polling the old job after expiry even if anonymous recovery fails", async () => {
  const api = server();
  vi.mocked(fetch).mockReset();
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const path = String(input);
    api.requests.push({ path, method: init?.method ?? "GET" });
    await new Promise((resolve) => setTimeout(resolve, 1));
    const first = api.requests.length === 1;
    const upload = path === "/ui-api/analyze";
    return new Response(
      JSON.stringify(first ? session : upload ? { job_id: "old-job" } : {}),
      {
        status: first ? 200 : upload ? 202 : 401,
      },
    );
  });
  const view = render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
  await tick();
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [file()] },
  });
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  await tick(2000);
  expect(
    screen.getByRole("button", { name: "Повторить подключение" }),
  ).toBeTruthy();
  for (let i = 0; i < 60; i++) {
    await tick(1000);
    expect(api.posts()).toBe(2);
    expect(api.gets()).toBe(0);
    expect(
      api.requests.filter(
        (r) => r.path === "/ui-api/jobs/old-job?include_details=true",
      ),
    ).toHaveLength(1);
    expect(
      api.requests.filter((r) => r.path === "/ui-api/analyze"),
    ).toHaveLength(1);
  }
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});

it("times out bootstrap into a separate error screen without retrying", async () => {
  vi.useFakeTimers();
  vi.stubEnv("VITE_UI_MODE", "bridge");
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(
    (_input, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(init.signal?.reason),
          { once: true },
        );
      }),
  );
  const view = render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
  await tick(10001);
  expect(screen.queryByLabelText("MOV-видео")).toBeNull();
  expect(screen.getByRole("alert")).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Повторить подключение" }),
  ).toBeTruthy();
  await tick(60000);
  expect(fetch).toHaveBeenCalledOnce();
  expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});
