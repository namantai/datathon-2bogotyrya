export interface EvidenceFrame {
  timestamp_sec: number;
  image_base64: string;
}
export interface Violation {
  type: string;
  timecode_sec: number;
  description: string;
  evidence_frames: EvidenceFrame[];
}
export interface DoneJob {
  job_id: string;
  status: "done";
  verdict: "violation" | "no_violation";
  violations: Violation[];
}
export type Job = { status: "processing" } | { status: "failed" } | DoneJob;
export type Scenario = "none" | "one" | "multiple" | "error";
// A trusted future bridge supplies sources separately from the jury JSON.
export interface RuleSource {
  title: string;
  url: string;
  section?: string;
}
export interface ResultViewModel {
  result: DoneJob;
  sources?: Record<number, RuleSource>;
}
export interface AnalysisService {
  readonly mode: "demo" | "bridge";
  submit(file: File, signal: AbortSignal, scenario: Scenario): Promise<unknown>;
  getJob(jobId: string, signal: AbortSignal): Promise<unknown>;
}
export class UiError extends Error {}
const invalid = () =>
  new UiError("Сервис вернул некорректный ответ. Анализ не завершён.");
const object = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const text = (v: unknown): v is string =>
  typeof v === "string" && v.trim().length > 0;
const time = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v) && v >= 0;
export function parseAccepted(v: unknown): string {
  if (!object(v) || !text(v.job_id)) throw invalid();
  return v.job_id;
}
export function parseJob(v: unknown, jobId: string): Job {
  if (!object(v)) throw invalid();
  if (v.status === "processing" || v.status === "failed")
    return { status: v.status };
  if (
    v.status !== "done" ||
    v.job_id !== jobId ||
    !Array.isArray(v.violations) ||
    (v.verdict !== "violation" && v.verdict !== "no_violation") ||
    (v.verdict === "no_violation") !== (v.violations.length === 0)
  )
    throw invalid();
  const violations = v.violations.map((event: unknown): Violation => {
    if (
      !object(event) ||
      !text(event.type) ||
      !text(event.description) ||
      !time(event.timecode_sec) ||
      !Array.isArray(event.evidence_frames) ||
      event.evidence_frames.length < 1 ||
      event.evidence_frames.length > 3
    )
      throw invalid();
    const frames = event.evidence_frames.map(
      (frame: unknown): EvidenceFrame => {
        if (
          !object(frame) ||
          !time(frame.timestamp_sec) ||
          typeof frame.image_base64 !== "string"
        )
          throw invalid();
        return {
          timestamp_sec: frame.timestamp_sec,
          image_base64: frame.image_base64,
        };
      },
    );
    return {
      type: event.type,
      timecode_sec: event.timecode_sec,
      description: event.description,
      evidence_frames: frames,
    };
  });
  return { status: "done", job_id: jobId, verdict: v.verdict, violations };
}
export function jpegSource(value: string): string | null {
  if (
    !value ||
    value.length > 5_592_408 ||
    value.length % 4 !== 0 ||
    !/^[A-Za-z0-9+/]+={0,2}$/.test(value)
  )
    return null;
  try {
    const bytes = atob(value);
    if (!bytes.startsWith("\xff\xd8\xff") || !bytes.endsWith("\xff\xd9"))
      return null;
    return `data:image/jpeg;base64,${value}`;
  } catch {
    return null;
  }
}
export function safeSourceUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}
