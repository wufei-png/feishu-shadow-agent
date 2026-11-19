import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const withdrawnAdvisory = "GHSA-fc5q-39xc-vjpf";
const withdrawnUrl = `https://github.com/advisories/${withdrawnAdvisory}`;

// npm may continue reporting this advisory after GitHub has withdrawn it.
// Remove this exception when the npm audit feed no longer includes it.
export function requiresWithdrawnCheck(report, exitCode) {
  const vulnerabilities = report?.vulnerabilities;
  const counts = report?.metadata?.vulnerabilities;
  if (
    report?.error ||
    report?.auditReportVersion !== 2 ||
    !vulnerabilities ||
    typeof vulnerabilities !== "object" ||
    Array.isArray(vulnerabilities) ||
    !Number.isInteger(counts?.high) ||
    !Number.isInteger(counts?.critical) ||
    ![0, 1].includes(exitCode)
  ) {
    throw new Error("npm audit did not return a valid report");
  }

  const entries = Object.entries(vulnerabilities);
  const severities = ["info", "low", "moderate", "high", "critical"];
  const directHigh = [];
  for (const [name, item] of entries) {
    if (
      !item ||
      !severities.includes(item.severity) ||
      !Array.isArray(item.via)
    ) {
      throw new Error(`Invalid audit result for ${name}`);
    }
    for (const cause of item.via) {
      if (typeof cause === "string") continue;
      if (!cause || !severities.includes(cause.severity)) {
        throw new Error(`Invalid audit advisory for ${name}`);
      }
      if (["high", "critical"].includes(cause.severity)) directHigh.push(cause);
    }
  }
  const high = entries.filter(([, value]) => value?.severity === "high");
  const critical = entries.filter(
    ([, value]) => value?.severity === "critical",
  );
  if (
    counts.high !== high.length ||
    counts.critical !== critical.length ||
    (exitCode === 0) !== (high.length + critical.length === 0)
  ) {
    throw new Error("npm audit results are inconsistent");
  }
  if (critical.length) {
    throw new Error(
      `Critical vulnerabilities: ${critical.map(([name]) => name).join(", ")}`,
    );
  }
  const unexpected = directHigh.filter(
    (cause) =>
      cause.name !== "picomatch" ||
      cause.url !== withdrawnUrl ||
      cause.severity !== "high",
  );
  if (unexpected.length) {
    throw new Error(
      `Unresolved high or critical advisories: ${unexpected.map((cause) => `${cause.name}: ${cause.url}`).join(", ")}`,
    );
  }
  if (directHigh.length > 0 && high.length === 0) {
    throw new Error("Unresolved high advisory has inconsistent severity");
  }

  function highCauses(name, visiting = new Set()) {
    if (visiting.has(name)) throw new Error(`Cyclic audit dependency: ${name}`);
    const item = vulnerabilities[name];
    if (!item || !Array.isArray(item.via) || item.via.length === 0) {
      throw new Error(`Incomplete audit dependency: ${name}`);
    }
    const next = new Set(visiting).add(name);
    return item.via.flatMap((cause) => {
      if (typeof cause === "string") return highCauses(cause, next);
      if (!cause || !severities.includes(cause.severity)) {
        throw new Error(`Invalid audit advisory for ${name}`);
      }
      return ["high", "critical"].includes(cause.severity) ? [cause] : [];
    });
  }

  for (const [name] of high) {
    const causes = highCauses(name);
    if (
      !causes.length ||
      causes.some(
        (cause) =>
          cause.name !== "picomatch" ||
          cause.url !== withdrawnUrl ||
          cause.severity !== "high",
      )
    ) {
      throw new Error(`Unresolved high vulnerability: ${name}`);
    }
  }
  return high.length > 0;
}

export async function checkAudit(report, exitCode, isWithdrawn) {
  if (requiresWithdrawnCheck(report, exitCode) && !(await isWithdrawn())) {
    throw new Error(`${withdrawnAdvisory} is not confirmed withdrawn`);
  }
}

async function isAdvisoryWithdrawn() {
  const headers = {
    Accept: "application/vnd.github+json",
    "User-Agent": "feishu-shadow-agent-ci",
  };
  if (process.env.GITHUB_TOKEN)
    headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  const response = await fetch(
    `https://api.github.com/advisories/${withdrawnAdvisory}`,
    {
      headers,
      signal: AbortSignal.timeout(15000),
    },
  );
  if (!response.ok)
    throw new Error(`GitHub advisory lookup failed: HTTP ${response.status}`);
  const advisory = await response.json();
  return (
    advisory.ghsa_id === withdrawnAdvisory &&
    typeof advisory.withdrawn_at === "string" &&
    !Number.isNaN(Date.parse(advisory.withdrawn_at))
  );
}

async function main() {
  const cwd = fileURLToPath(
    new URL("../../frontend/operator-console/", import.meta.url),
  );
  const result = spawnSync(
    "npm",
    [
      "audit",
      "--json",
      "--audit-level=high",
      "--registry=https://registry.npmjs.org",
    ],
    { cwd, encoding: "utf8", maxBuffer: 10 * 1024 * 1024 },
  );
  if (result.error) throw result.error;
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch {
    throw new Error(`npm audit returned invalid JSON: ${result.stderr.trim()}`);
  }
  await checkAudit(report, result.status, isAdvisoryWithdrawn);
  console.log("npm audit summary:", report.metadata.vulnerabilities);
  if (report.metadata.vulnerabilities.high) {
    console.log(
      `Ignored only ${withdrawnAdvisory}, confirmed withdrawn by GitHub.`,
    );
  }
}

if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
