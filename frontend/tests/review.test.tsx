import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import App from "../src/App";
import { Results } from "../src/components/Results";
import { analyze } from "../src/lib/analysis";
import { parseJob } from "../src/lib/contracts";
import type { AnalysisService, DoneJob } from "../src/lib/contracts";
import { createBridgeService } from "../src/services/bridge";
import { done, file } from "./fixtures";

const response = (value: unknown) => new Response(JSON.stringify(value));
function extended() {
  return {
    ...done(1),
    event_details: [
      {
        event_code: "Тест 1",
        name: "Синтетическое событие",
        severity: "high",
        regulatory_references: [
          {
            status: "candidate",
            rule: {
              review_status: "verified",
              document_title: "Synthetic verified fixture, not a real law",
              source_url: "https://example.invalid/synthetic-rule",
              provision: "Synthetic section",
              requirement_summary: "Synthetic requirement",
              checked_at: "2026-10-18",
              revision_version: "test",
            },
          },
        ],
      },
    ],
  };
}

it("passes private metadata through the real bridge to risk and verified references", async () => {
  vi.useFakeTimers();
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(response({ job_id: "test-job" }))
    .mockResolvedValueOnce(response(extended()));
  const api = createBridgeService(
    {
      csrf_token: "synthetic-test-token",
      analyzer_mode: "local",
      max_upload_bytes: 1024,
    },
    vi.fn(),
  );
  render(<App service={api} />);
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [file()] },
  });
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(fetch.mock.lastCall?.[0]).toBe(
    "/ui-api/jobs/test-job?include_details=true",
  );
  expect(
    screen.getByRole("heading", { name: "Синтетическое событие" }),
  ).toBeTruthy();
  expect(screen.getByText(/Критичность: высокая/)).toBeTruthy();
  expect(screen.getByText(/не юридический вывод/)).toBeTruthy();
  const link = screen.getByRole("link", { name: /Synthetic verified fixture/ });
  expect(link.getAttribute("href")).toBe(
    "https://example.invalid/synthetic-rule",
  );
});

it.each([
  "pending_review",
  "insufficient_data",
  "not_applicable",
  "unsafe",
  "mismatch",
  "incomplete",
])("does not promote %s metadata to a verified legal basis", (variant) => {
  const raw = extended();
  const detail = raw.event_details[0],
    ref = detail.regulatory_references[0];
  if (variant === "pending_review") ref.rule.review_status = variant;
  else if (variant === "unsafe") ref.rule.source_url = "javascript:alert(1)";
  else if (variant === "mismatch") detail.event_code = "other-event";
  else if (variant === "incomplete") ref.rule.checked_at = "";
  else ref.status = variant;
  render(
    <Results
      model={{ result: parseJob(raw, "test-job") as DoneJob }}
      demo={false}
      onReset={() => {}}
    />,
  );
  expect(screen.queryByRole("link")).toBeNull();
  expect(screen.getByText(/Нормативное основание требует/)).toBeTruthy();
});

it("unmarked legacy sources are not treated as legally verified", () => {
  render(
    <Results
      model={{
        result: done(1),
        sources: { 0: { title: "Unverified", url: "https://example.invalid" } },
      }}
      demo={false}
      onReset={() => {}}
    />,
  );
  expect(screen.queryByRole("link")).toBeNull();
});

it("does not coerce malformed risk metadata to a severity", () => {
  const raw = extended();
  Object.assign(raw.event_details[0], { severity: ["high"] });
  const parsed = parseJob(raw, "test-job") as DoneJob;
  expect(parsed.eventDetails?.[0].severity).toBe("undetermined");
});

it("server timeout is distinct from an analyzer error and stops polling", async () => {
  const api: AnalysisService = {
    mode: "bridge",
    submit: vi.fn().mockResolvedValue({ job_id: "test-job" }),
    getJob: vi
      .fn()
      .mockResolvedValue({ status: "failed", failure_reason: "timeout" }),
  };
  await expect(
    analyze(api, file(), "none", new AbortController().signal, vi.fn(), {
      pollMs: 1,
      timeoutMs: 1000,
    }),
  ).rejects.toThrow("Превышен серверный лимит");
  expect(api.getJob).toHaveBeenCalledOnce();
});

it("rejects a late response even before the throttled timeout callback executes", async () => {
  let elapsed = 0;
  vi.spyOn(performance, "now").mockImplementation(() => elapsed);
  const api: AnalysisService = {
    mode: "bridge",
    submit: vi.fn().mockResolvedValue({ job_id: "test-job" }),
    getJob: vi.fn().mockImplementation(async () => {
      elapsed = 300_000;
      return done();
    }),
  };
  await expect(
    analyze(api, file(), "none", new AbortController().signal, vi.fn(), {
      pollMs: 1,
      timeoutMs: 300_000,
    }),
  ).rejects.toThrow("Время ожидания истекло");
});

it("uses monotonic elapsed time when the system clock jumps", async () => {
  vi.useFakeTimers();
  const api: AnalysisService = {
    mode: "bridge",
    submit: () => new Promise(() => {}),
    getJob: vi.fn(),
  };
  render(<App service={api} />);
  fireEvent.change(screen.getByLabelText("MOV-видео"), {
    target: { files: [file()] },
  });
  fireEvent.click(screen.getByRole("button", { name: "Начать анализ" }));
  vi.setSystemTime(Date.now() + 3_600_000);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(screen.getByRole("timer").textContent).toBe("00:01 / 05:00");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(299_000);
  });
  expect(screen.getByRole("alert").textContent).toContain(
    "Время ожидания истекло",
  );
  expect(screen.queryByRole("timer")).toBeNull();
});

it("shows an unavailable-evidence warning instead of an invented frame", () => {
  const result = done(1);
  result.violations[0].evidence_frames[0].image_base64 = "";
  render(<Results model={{ result }} demo={false} onReset={() => {}} />);
  expect(screen.queryByRole("img")).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("Кадр недоступен");
});
