import { expect, it } from "vitest";
import { intentTimeout, idleMinutes } from "./routingDurations";
it("accepts integer intent timeouts only within 30 to 180 seconds", () => {
  expect(intentTimeout("90")).toBe(90);
  expect(intentTimeout("30")).toBe(30);
  expect(intentTimeout("180")).toBe(180);
  for (const value of ["", "29", "181", "90.5", "NaN"]) expect(intentTimeout(value)).toBeNull();
});
it("converts custom residence minutes and preserves manual unload", () => {
  expect(idleMinutes("0")).toBe(0);
  expect(idleMinutes("30")).toBe(1800);
  expect(idleMinutes("1440")).toBe(86400);
  for (const value of ["", "-1", "1.5", "1441"]) expect(idleMinutes(value)).toBeNull();
});
