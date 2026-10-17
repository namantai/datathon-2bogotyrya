import { expect, it, vi } from "vitest";
import { analyze } from "../src/lib/analysis";
import type { AnalysisService } from "../src/lib/contracts";
import { done, file } from "./fixtures";
function service(): AnalysisService {
  return {
    mode: "bridge",
    submit: vi.fn().mockResolvedValue({ job_id: "test-job" }),
    getJob: vi.fn().mockResolvedValue(done()),
  };
}
it("polls sequentially and stops after done", async () => {
  vi.useFakeTimers();
  const api = service();
  const onAccepted = vi.fn();
  vi.mocked(api.getJob).mockResolvedValueOnce({ status: "processing" });
  const run = analyze(
    api,
    file(),
    "none",
    new AbortController().signal,
    onAccepted,
  );
  await vi.advanceTimersByTimeAsync(3000);
  await expect(run).resolves.toEqual(done());
  await vi.advanceTimersByTimeAsync(30_000);
  expect(onAccepted).toHaveBeenCalledWith("test-job");
  expect(api.getJob).toHaveBeenCalledTimes(2);
  expect(vi.getTimerCount()).toBe(0);
});
it.each(["upload", "poll"])("times out a hung %s", async (stage) => {
  vi.useFakeTimers();
  const api = service();
  vi.mocked(stage === "upload" ? api.submit : api.getJob).mockImplementation(
    () => new Promise(() => {}),
  );
  const settled = analyze(
    api,
    file(),
    "none",
    new AbortController().signal,
    () => {},
  ).catch((e) => e);
  await vi.advanceTimersByTimeAsync(300_000);
  expect((await settled).message).toContain("Время ожидания истекло");
  expect(vi.getTimerCount()).toBe(0);
});
it("cancels polling and cleans timers", async () => {
  vi.useFakeTimers();
  const api = service(),
    controller = new AbortController();
  const run = analyze(api, file(), "none", controller.signal, () => {}).catch(
    (e) => e,
  );
  await vi.advanceTimersByTimeAsync(10);
  controller.abort();
  await run;
  await vi.advanceTimersByTimeAsync(6000);
  expect(api.getJob).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});
it.each([{ status: "failed" }, { status: "done", verdict: "unknown" }])(
  "stops on failure %j",
  async (response) => {
    vi.useFakeTimers();
    const api = service();
    vi.mocked(api.getJob).mockResolvedValue(response);
    const run = analyze(
      api,
      file(),
      "none",
      new AbortController().signal,
      () => {},
    ).catch((e) => e);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(await run).toBeInstanceOf(Error);
    expect(api.getJob).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  },
);
