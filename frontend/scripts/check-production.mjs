import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
const assets = readdirSync(new URL("../dist/assets/", import.meta.url));
const js = assets
  .filter((name) => name.endsWith(".js"))
  .map((name) =>
    readFileSync(new URL(`../dist/assets/${name}`, import.meta.url), "utf8"),
  )
  .join("\n");
for (const forbidden of [
  "СИНТЕТИЧЕСКИЙ ТЕСТ",
  "Не является кадром загруженного видео",
  "X-API-Key",
  "/api/v1/analyze",
  "localStorage",
  "sessionStorage",
])
  assert.ok(
    !js.includes(forbidden),
    `Forbidden production content: ${forbidden}`,
  );
assert.ok(
  !assets.some((name) => /^mock-/.test(name)),
  "Mock chunk must not ship",
);
console.log(
  "Production bundle: mock implementation and protected API access absent.",
);
