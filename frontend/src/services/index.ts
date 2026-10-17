import type { AnalysisService } from "../lib/contracts";
export async function loadService(): Promise<AnalysisService | null> {
  if (import.meta.env.DEV && import.meta.env.VITE_UI_MODE === "mock") {
    const { createMockService } = await import("./mock");
    return createMockService();
  }
  // null selects the anonymous same-origin bridge, including production.
  return null;
}
