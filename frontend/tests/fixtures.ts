import type { DoneJob } from "../src/lib/contracts";
// Generated flat-color 8x8 JPEG, exclusively synthetic test data.
export const JPEG =
  "/9j/4AAQSkZJRgABAgAAAQABAAD//gAPTGF2YzYzLjEuMTAyAP/bAEMACAoKCwoLDQ0NDQ0NEA8QEBAQEBAQEBAQEBISEhUVFRISEhAQEhIUFBUVFxcXFRUVFRcXGRkZHh4cHCMjJCsrM//EAEsAAQEAAAAAAAAAAAAAAAAAAAAIAQEAAAAAAAAAAAAAAAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAEQEAAAAAAAAAAAAAAAAAAAAA/8AAEQgACAAIAwEiAAIRAAMRAP/aAAwDAQACEQMRAD8An8AH/9k=";
export const file = () =>
  new File(["synthetic-test-only"], "sample.MOV", {
    type: "application/octet-stream",
  });
export function done(count = 0): DoneJob {
  return {
    job_id: "test-job",
    status: "done",
    verdict: count ? "violation" : "no_violation",
    violations: Array.from({ length: count }, (_, i) => ({
      type: `Тест ${i + 1}`,
      description: "Синтетическое наблюдение",
      timecode_sec: 12.4 + i,
      evidence_frames: [{ timestamp_sec: 12.425 + i, image_base64: JPEG }],
    })),
  };
}
