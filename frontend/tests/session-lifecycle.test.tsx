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

function server(initiallyAuthenticated = false) {
  vi.useFakeTimers();
  vi.stubEnv("VITE_UI_MODE", "bridge");
  let authenticated = initiallyAuthenticated;
  const requests: { path: string; method: string }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    requests.push({ path, method });
    // Async responses make a retry/render loop observable without hanging act().
    await new Promise((resolve) => setTimeout(resolve, 1));
    if (path === "/ui-api/session" && method === "POST") authenticated = true;
    if (path === "/ui-api/session" && method === "DELETE") {
      authenticated = false;
      return new Response(null, { status: 204 });
    }
    return new Response(
      JSON.stringify(authenticated ? session : { detail: "Unauthorized" }),
      {
        status: authenticated ? 200 : 401,
      },
    );
  });
  return {
    requests,
    gets: () =>
      requests.filter((r) => r.path === "/ui-api/session" && r.method === "GET")
        .length,
    expire: () => {
      authenticated = false;
    },
  };
}

async function tick() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
}

async function idleWithoutSessionRetries(gets: () => number, expected: number) {
  for (let i = 0; i < 60; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(gets()).toBe(expected);
  }
}

it.each([false, true])(
  "bounds initial 401 session checks across idle time and rerenders (StrictMode=%s)",
  async (strict) => {
    const api = server();
    const tree = () =>
      strict ? (
        <StrictMode>
          <App />
        </StrictMode>
      ) : (
        <App />
      );
    const view = render(tree());
    await tick();
    await tick();
    const expected = strict ? 2 : 1;
    expect(api.gets()).toBe(expected);
    expect(screen.getByRole("button", { name: "Войти" })).toBeTruthy();
    for (let i = 0; i < 20; i++) {
      view.rerender(tree());
      fireEvent.change(screen.getByLabelText("Код доступа Web UI"), {
        target: { value: `synthetic-${i}` },
      });
      await tick();
      expect(api.gets()).toBe(expected);
    }
    await idleWithoutSessionRetries(api.gets, expected);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  },
);

it("keeps POST login state, recognizes a session on reopening, and stops after expiry", async () => {
  const api = server();
  const tree = (
    <StrictMode>
      <App />
    </StrictMode>
  );
  let view = render(tree);
  await tick();
  await tick();
  expect(api.gets()).toBe(2);
  fireEvent.change(screen.getByLabelText("Код доступа Web UI"), {
    target: { value: "synthetic-ui-code-only" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));
  await tick();
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(1);
  expect(screen.getByLabelText("MOV-видео")).toBeTruthy();
  await idleWithoutSessionRetries(api.gets, 2);
  view.unmount();
  view = render(tree);
  await tick();
  await tick();
  expect(api.gets()).toBe(4);
  expect(screen.getByLabelText("MOV-видео")).toBeTruthy();
  api.expire();
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [file()] },
  });
  fireEvent.click(screen.getByRole("button", { name: "Начать проверку" }));
  await tick();
  expect(screen.getByRole("alert").textContent).toContain("Сессия истекла");
  expect(screen.getByRole("button", { name: "Войти" })).toBeTruthy();
  await idleWithoutSessionRetries(api.gets, 4);
  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});
