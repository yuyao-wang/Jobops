export const PROTOCOL_NAME = "jobops.node-worker";
export const PROTOCOL_VERSION = 1;
export const WORKER_VERSION = "0.1.0";
export const MAX_REQUEST_BYTES = 64 * 1024;

export class RequestError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "RequestError";
    this.code = code;
  }
}

export function validateRequest(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new RequestError("INVALID_REQUEST", "Request must be a JSON object");
  }
  if (value.protocol !== PROTOCOL_NAME) {
    throw new RequestError("PROTOCOL_MISMATCH", "Unsupported protocol");
  }
  if (value.version !== PROTOCOL_VERSION) {
    throw new RequestError("VERSION_MISMATCH", "Unsupported protocol version");
  }
  if (typeof value.id !== "string" || value.id.length === 0 || value.id.length > 128) {
    throw new RequestError("INVALID_REQUEST", "Request id must be a non-empty string");
  }
  if (typeof value.method !== "string" || value.method.length === 0) {
    throw new RequestError("INVALID_REQUEST", "Request method must be a non-empty string");
  }
  if (
    value.params === null ||
    typeof value.params !== "object" ||
    Array.isArray(value.params)
  ) {
    throw new RequestError("INVALID_REQUEST", "Request params must be a JSON object");
  }
  return value;
}

export function successResponse(id, result) {
  return {
    protocol: PROTOCOL_NAME,
    version: PROTOCOL_VERSION,
    id,
    ok: true,
    result,
  };
}

export function errorResponse(id, error) {
  const known = error instanceof RequestError;
  return {
    protocol: PROTOCOL_NAME,
    version: PROTOCOL_VERSION,
    id: typeof id === "string" ? id : null,
    ok: false,
    error: {
      code: known ? error.code : "INTERNAL_ERROR",
      message: known ? error.message : "Worker request failed",
    },
  };
}
