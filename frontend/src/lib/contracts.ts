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
  // Normalized private UI metadata, not part of the jury JSON contract.
  eventDetails?: EventDetail[];
}
export type Job =
  | { status: "processing" }
  | { status: "failed"; failureReason?: "timeout" }
  | DoneJob;
export type Scenario = "none" | "one" | "multiple" | "error";
// A trusted future bridge supplies sources separately from the jury JSON.
export interface RuleSource {
  title: string;
  url: string;
  section?: string;
  verified?: boolean;
}
export type Severity = "undetermined" | "low" | "medium" | "high" | "critical";
export interface EventDetail {
  name: string;
  severity: Severity;
  sources: RuleSource[];
}
export interface ResultViewModel {
  result: DoneJob;
  sources?: Record<number, RuleSource>;
}
export interface AnalysisService {
  readonly mode: "demo" | "bridge";
  readonly analyzerMode?: "fake" | "disabled" | "local";
  readonly maxUploadBytes?: number;
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
  if (v.status === "processing") return { status: v.status };
  if (v.status === "failed")
    return v.failure_reason === "timeout"
      ? { status: "failed", failureReason: "timeout" }
      : { status: "failed" };
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
  const result: DoneJob = {
    status: "done",
    job_id: jobId,
    verdict: v.verdict,
    violations,
  };
  if (
    Array.isArray(v.event_details) &&
    v.event_details.length === violations.length
  ) {
    result.eventDetails = v.event_details.map(
      (detail: unknown, index): EventDetail => {
        const fallback: EventDetail = {
          name: violations[index].type,
          severity: "undetermined",
          sources: [],
        };
        if (!object(detail) || detail.event_code !== violations[index].type)
          return fallback;
        const severity =
          typeof detail.severity === "string" &&
          ["low", "medium", "high", "critical"].includes(detail.severity)
            ? (detail.severity as Severity)
            : "undetermined";
        const sources: RuleSource[] = [];
        if (Array.isArray(detail.regulatory_references)) {
          for (const ref of detail.regulatory_references) {
            if (!object(ref) || ref.status !== "candidate" || !object(ref.rule))
              continue;
            const rule = ref.rule;
            if (
              rule.review_status !== "verified" ||
              !text(rule.document_title) ||
              !text(rule.requirement_summary) ||
              !text(rule.checked_at) ||
              !text(rule.revision_version) ||
              !text(rule.source_url)
            )
              continue;
            const url = safeSourceUrl(rule.source_url);
            if (url)
              sources.push({
                title: rule.document_title,
                url,
                verified: true,
                ...(text(rule.provision) ? { section: rule.provision } : {}),
              });
          }
        }
        return {
          name: text(detail.name) ? detail.name : fallback.name,
          severity,
          sources,
        };
      },
    );
  }
  return result;
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
