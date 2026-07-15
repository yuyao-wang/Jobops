#!/usr/bin/env node

import readline from "node:readline";

import { ADAPTERS, probeUrl } from "./lib/probe.mjs";
import {
  MAX_REQUEST_BYTES,
  PROTOCOL_NAME,
  PROTOCOL_VERSION,
  RequestError,
  WORKER_VERSION,
  errorResponse,
  successResponse,
  validateRequest,
} from "./lib/protocol.mjs";

function capabilities() {
  return {
    protocol: PROTOCOL_NAME,
    protocol_version: PROTOCOL_VERSION,
    worker_version: WORKER_VERSION,
    methods: ["capabilities", "probe_url"],
    adapters: ADAPTERS,
    runtime: {
      name: "node",
      version: process.versions.node,
    },
  };
}

function dispatch(request) {
  switch (request.method) {
    case "capabilities":
      return capabilities();
    case "probe_url":
      return probeUrl(request.params.url);
    default:
      throw new RequestError("METHOD_NOT_FOUND", "Unsupported request method");
  }
}

function writeResponse(response) {
  process.stdout.write(`${JSON.stringify(response)}\n`);
}

const input = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
  terminal: false,
});

input.on("line", (line) => {
  let id = null;
  try {
    if (Buffer.byteLength(line, "utf8") > MAX_REQUEST_BYTES) {
      throw new RequestError("REQUEST_TOO_LARGE", "Request exceeds size limit");
    }
    let decoded;
    try {
      decoded = JSON.parse(line);
    } catch {
      throw new RequestError("INVALID_JSON", "Request is not valid JSON");
    }
    id = decoded?.id ?? null;
    const request = validateRequest(decoded);
    writeResponse(successResponse(request.id, dispatch(request)));
  } catch (error) {
    writeResponse(errorResponse(id, error));
  }
});

// Do not log request bodies, URLs, environment values, or stack traces. Protocol
// responses on stdout are the worker's only observable output.
