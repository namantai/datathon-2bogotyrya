import type { AnalysisService, DoneJob, Scenario } from "../lib/contracts";
import { pause } from "../lib/analysis";

function syntheticJpeg(index: number): string {
  const canvas = document.createElement("canvas");
  canvas.width = 640;
  canvas.height = 360;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Canvas unavailable");
  ctx.fillStyle = "#e6ebe7";
  ctx.fillRect(0, 0, 640, 360);
  ctx.strokeStyle = "#c8d3cc";
  for (let x = 0; x < 640; x += 40) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, 360);
    ctx.stroke();
  }
  for (let y = 0; y < 360; y += 40) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(640, y);
    ctx.stroke();
  }
  ctx.fillStyle = "#718b7a";
  ctx.fillRect(180 + index * 30, 120, 130, 90);
  ctx.fillStyle = "#b78b57";
  ctx.beginPath();
  ctx.arc(420, 165, 35, 0, 2 * Math.PI);
  ctx.fill();
  ctx.fillStyle = "#183c30";
  ctx.fillRect(0, 280, 640, 80);
  ctx.fillStyle = "#fff";
  ctx.font = "bold 24px sans-serif";
  ctx.fillText("СИНТЕТИЧЕСКИЙ ТЕСТ", 28, 316);
  ctx.font = "16px sans-serif";
  ctx.fillText("Не является кадром загруженного видео", 28, 343);
  return canvas.toDataURL("image/jpeg", 0.8).split(",")[1];
}
export function createMockService(): AnalysisService {
  if (!import.meta.env.DEV)
    throw new Error("Demo is unavailable in production");
  let current: { id: string; scenario: Scenario; polls: number } | undefined;
  return {
    mode: "demo",
    async submit(_file, signal, scenario) {
      await pause(700, signal);
      current = { id: `demo-${crypto.randomUUID()}`, scenario, polls: 0 };
      return { job_id: current.id };
    },
    async getJob(jobId, signal) {
      signal.throwIfAborted();
      if (!current || current.id !== jobId) throw new Error("Unknown demo job");
      if (++current.polls < 2) return { status: "processing" };
      if (current.scenario === "error") return { status: "failed" };
      const count =
        current.scenario === "none" ? 0 : current.scenario === "one" ? 1 : 2;
      const result: DoneJob = {
        job_id: jobId,
        status: "done",
        verdict: count ? "violation" : "no_violation",
        violations: Array.from({ length: count }, (_, index) => ({
          type: `Тестовое событие ${index + 1}`,
          timecode_sec: index ? 74.125 : 12.4,
          description:
            "Синтетический пример: геометрические фигуры используются для проверки интерфейса. Это не наблюдение по выбранному видео и не вывод AI.",
          evidence_frames: Array.from(
            { length: index ? 1 : 3 },
            (_, frame) => ({
              timestamp_sec: (index ? 74.125 : 12.4) + frame * 0.2,
              image_base64: syntheticJpeg(frame),
            }),
          ),
        })),
      };
      return result;
    },
  };
}
