import type { AnalysisService } from "../lib/contracts";
export async function loadService(): Promise<AnalysisService | null> {
  if (import.meta.env.DEV) {
    const { createMockService } = await import("./mock");
    return createMockService();
  }
  // TASK 04 has no route to the protected jury API.
  return null;
}
