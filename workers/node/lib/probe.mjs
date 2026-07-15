import { RequestError } from "./protocol.mjs";

export const ADAPTERS = Object.freeze([
  "greenhouse",
  "lever",
  "ashby",
  "jobvite",
  "workday",
  "generic",
]);

function hostnameIs(hostname, expected) {
  return hostname === expected || hostname.endsWith(`.${expected}`);
}

function classifyHostname(hostname) {
  if (
    hostname === "boards.greenhouse.io" ||
    hostname === "job-boards.greenhouse.io" ||
    hostname === "boards.eu.greenhouse.io" ||
    hostname === "job-boards.eu.greenhouse.io"
  ) {
    return "greenhouse";
  }
  if (hostname === "jobs.lever.co" || hostname === "jobs.eu.lever.co") {
    return "lever";
  }
  if (hostname === "jobs.ashbyhq.com") {
    return "ashby";
  }
  if (hostnameIs(hostname, "jobvite.com")) {
    return "jobvite";
  }
  if (
    hostnameIs(hostname, "myworkdayjobs.com") ||
    hostnameIs(hostname, "myworkdaysite.com") ||
    hostnameIs(hostname, "workdayjobs.com")
  ) {
    return "workday";
  }
  return "generic";
}

export function probeUrl(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > 8192) {
    throw new RequestError("INVALID_URL", "URL must be a non-empty string");
  }

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new RequestError("INVALID_URL", "URL is not valid");
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new RequestError("INVALID_URL", "Only HTTP and HTTPS URLs are supported");
  }
  if (parsed.username || parsed.password) {
    throw new RequestError("INVALID_URL", "URLs containing credentials are not supported");
  }

  const adapter = classifyHostname(parsed.hostname.toLowerCase());
  return {
    adapter,
    supported: adapter !== "generic",
    deterministic: true,
    match_basis: "hostname",
  };
}
