# Security Policy

## Reporting

If you discover a security vulnerability in Memstem, please **do not open a public issue.**

Email: bbesner@techprosecurity.com
Subject line: `[Memstem Security] <brief description>`

I'll acknowledge within 48 hours and provide a remediation timeline within 5 business days.

## Scope

In-scope:

- Vulnerabilities in the Memstem daemon, CLI, MCP server, or HTTP API
- Vulnerabilities in adapters that read external filesystems
- Path traversal, injection, deserialization issues
- Embedding model exfiltration / prompt injection via memory content

Out-of-scope:

- Vulnerabilities in upstream dependencies (please report to those projects)
- Vulnerabilities in the AI clients Memstem connects to
- DoS via legitimate API usage at high volume

## Supported versions

Pre-1.0: only the latest minor version is supported.
