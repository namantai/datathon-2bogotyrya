import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
// jsdom has no decoder or blob playback. Behaviour is tested; browser support is separate.
Object.defineProperty(URL, "createObjectURL", {
  configurable: true,
  writable: true,
  value: () => "blob:synthetic",
});
Object.defineProperty(URL, "revokeObjectURL", {
  configurable: true,
  writable: true,
  value: () => {},
});
beforeEach(() => {
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:synthetic");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllEnvs();
});
