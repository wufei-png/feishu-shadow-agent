import assert from "node:assert/strict";
import test from "node:test";
import { checkAudit } from "./frontend-audit.mjs";

const withdrawn = {
  name: "picomatch",
  severity: "high",
  url: "https://github.com/advisories/GHSA-fc5q-39xc-vjpf",
};
const moderate = {
  name: "minimatch",
  severity: "moderate",
  url: "https://github.com/advisories/GHSA-g3hv-75wg-m249",
};

function report(vulnerabilities) {
  const entries = Object.values(vulnerabilities);
  return {
    auditReportVersion: 2,
    vulnerabilities,
    metadata: {
      vulnerabilities: {
        high: entries.filter((item) => item.severity === "high").length,
        critical: entries.filter((item) => item.severity === "critical").length,
      },
    },
  };
}

const staleReport = () =>
  report({
    picomatch: { severity: "high", via: [withdrawn] },
    minimatch: { severity: "moderate", via: [moderate] },
    tinyglobby: { severity: "high", via: ["picomatch"] },
    vite: { severity: "high", via: ["tinyglobby", "minimatch"] },
  });

test("permits a clean audit without querying GitHub", async () => {
  await checkAudit(
    report({ minimatch: { severity: "moderate", via: [moderate] } }),
    0,
    () => {
      throw new Error("unexpected lookup");
    },
  );
});

test("permits the stale advisory only when GitHub confirms withdrawal", async () => {
  await checkAudit(staleReport(), 1, async () => true);
  await assert.rejects(
    checkAudit(staleReport(), 1, async () => false),
    /not confirmed withdrawn/,
  );
  await assert.rejects(
    checkAudit(staleReport(), 1, async () => {
      throw new Error("lookup failed");
    }),
    /lookup failed/,
  );
});

test("blocks a new high or critical advisory, including on the same dependency", async () => {
  const extra = staleReport();
  extra.vulnerabilities.picomatch.via.push({
    ...withdrawn,
    url: "https://github.com/advisories/GHSA-other",
  });
  await assert.rejects(
    checkAudit(extra, 1, async () => true),
    /Unresolved high/,
  );

  const critical = report({
    library: {
      severity: "critical",
      via: [{ ...withdrawn, severity: "critical" }],
    },
  });
  await assert.rejects(
    checkAudit(critical, 1, async () => true),
    /Critical vulnerabilities/,
  );
});

test("fails closed for audit errors, inconsistent counts and incomplete causes", async () => {
  await assert.rejects(
    checkAudit({ error: { code: "EAUDIT" } }, 1, async () => true),
    /valid report/,
  );
  await assert.rejects(
    checkAudit(staleReport(), 2, async () => true),
    /valid report/,
  );
  const inconsistent = staleReport();
  inconsistent.metadata.vulnerabilities.high = 0;
  await assert.rejects(
    checkAudit(inconsistent, 1, async () => true),
    /inconsistent/,
  );
  const missing = report({ vite: { severity: "high", via: ["missing"] } });
  await assert.rejects(
    checkAudit(missing, 1, async () => true),
    /Incomplete/,
  );
  const cycle = report({
    a: { severity: "high", via: ["b"] },
    b: { severity: "high", via: ["a"] },
  });
  await assert.rejects(
    checkAudit(cycle, 1, async () => true),
    /Cyclic/,
  );
  const hiddenHigh = report({ x: { severity: "moderate", via: [withdrawn] } });
  await assert.rejects(
    checkAudit(hiddenHigh, 0, async () => true),
    /Unresolved high/,
  );
});
