import { describe, expect, it } from "vitest";
import { bootstrapTokenFromHash } from "./consoleSession";

describe("bootstrapTokenFromHash", () => {
  it("reads and decodes a token from the URL fragment", () => {
    expect(bootstrapTokenFromHash("#token=secret%2Bvalue%2Fpart")).toBe(
      "secret+value/part"
    );
  });

  it("does not treat SPA routes or empty values as tokens", () => {
    expect(bootstrapTokenFromHash("#dashboard")).toBeNull();
    expect(bootstrapTokenFromHash("#token=")).toBeNull();
  });
});
