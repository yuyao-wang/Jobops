# Jobops Node Worker

This directory contains a deliberately small Node.js bridge for the Python
orchestrator. It is groundwork for reusing Node-only browser components; it is
not a second implementation of the ATS adapters.

The worker speaks a versioned JSON-lines protocol over standard input and
standard output. Version 1 exposes two deterministic methods:

- `capabilities` reports protocol, runtime, and adapter-probe support.
- `probe_url` classifies Greenhouse, Lever, Ashby, Jobvite, Workday, or the
  generic fallback from the URL hostname.

No npm install is required. The worker uses only Node.js standard-library APIs
and supports Node.js 18 or newer.

```bash
node worker.mjs <<'EOF'
{"protocol":"jobops.node-worker","version":1,"id":"demo","method":"capabilities","params":{}}
EOF
```

Requests, including URLs or future sensitive values, are sent through stdin and
never command-line arguments. The worker never logs request bodies, URLs,
environment values, or stack traces. Responses contain classifications and
sanitized errors only.
