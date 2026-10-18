import type { AnalysisService } from "../lib/contracts";
export async function loadService(): Promise<AnalysisService | null> {
  // The application always uses the same-origin backend. Synthetic services
  // remain explicit test dependencies, never an environment-triggered fallback.
  return null;
}
