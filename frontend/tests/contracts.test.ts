import { describe, expect, it } from "vitest";
import {
  jpegSource,
  parseAccepted,
  parseJob,
  safeSourceUrl,
} from "../src/lib/contracts";
import {
  formatTime,
  MAX_UPLOAD_BYTES,
  validateFile,
} from "../src/lib/analysis";
import { done, file, JPEG } from "./fixtures";
describe("jury response boundary", () => {
  it.each([0, 1, 2])("accepts %i events", (count) =>
    expect(parseJob(done(count), "test-job")).toEqual(done(count)),
  );
  it.each([null, {}, { job_id: "" }, { job_id: 3 }, "invalid json"])(
    "rejects missing job_id: %j",
    (value) => expect(() => parseAccepted(value)).toThrow(),
  );
  it.each([
    null,
    {},
    "{",
    { status: "queued" },
    { ...done(), verdict: "unknown" },
    { ...done(), job_id: "other" },
    { ...done(1), verdict: "no_violation" },
    { ...done(), verdict: "violation" },
    { ...done(), violations: [{}] },
  ])("rejects invalid response %j", (value) =>
    expect(() => parseJob(value, "test-job")).toThrow(),
  );
  it.each([-1, NaN, Infinity, "12", true])(
    "rejects invalid time %s",
    (time) => {
      const result = done(1);
      Object.assign(result.violations[0], { timecode_sec: time });
      expect(() => parseJob(result, "test-job")).toThrow();
    },
  );
  it.each([0, 4])("rejects %i evidence frames", (count) => {
    const result = done(1);
    result.violations[0].evidence_frames = Array(count).fill(
      result.violations[0].evidence_frames[0],
    );
    expect(() => parseJob(result, "test-job")).toThrow();
  });
});
it("accepts .MOV irrespective of MIME and rejects empty, oversized and disguised files", () => {
  expect(() => validateFile(file())).not.toThrow();
  for (const f of [
    new File([], "empty.mov"),
    new File(["x"], "bad.exe", { type: "video/quicktime" }),
    { name: "large.mov", size: MAX_UPLOAD_BYTES + 1 } as File,
  ])
    expect(() => validateFile(f)).toThrow();
  expect(() =>
    validateFile({ name: "boundary.mov", size: MAX_UPLOAD_BYTES } as File),
  ).not.toThrow();
});
it.each([
  [0, "00:00"],
  [44.2, "00:44.2"],
  [74.125, "01:14.125"],
  [3600.005, "01:00:00.005"],
  [59.9999, "01:00"],
])("formats %s as %s", (input, expected) =>
  expect(formatTime(input as number)).toBe(expected),
);
it("permits only bounded JPEG data and safe source links", () => {
  expect(jpegSource(JPEG)).toBe(`data:image/jpeg;base64,${JPEG}`);
  for (const value of [
    "",
    "broken",
    "data:image/jpeg;base64," + JPEG,
    btoa("<svg onload=alert(1)>"),
    "A".repeat(5_592_412),
  ])
    expect(jpegSource(value)).toBeNull();
  expect(safeSourceUrl("https://example.org/rule")).toBe(
    "https://example.org/rule",
  );
  for (const value of [
    "javascript:alert(1)",
    "http://example.org",
    "https://user:pass@example.org",
    "//example.org",
  ])
    expect(safeSourceUrl(value)).toBeNull();
});
