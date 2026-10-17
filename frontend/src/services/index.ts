import type { AnalysisService } from "../lib/contracts";
export async function loadService(): Promise<AnalysisService | null> {
  if (import.meta.env.DEV && import.meta.env.VITE_UI_MODE === "mock") {
    const { createMockService } = await import("./mock");
    return createMockService();
  }
  // null selects the authenticated same-origin AccessGate, including production.
  return null;
}
