# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.x (latest release) | ✅ |
| older releases | ❌ |

## Reporting a Vulnerability

Please report vulnerabilities privately via
[GitHub Security Advisories](https://github.com/portolan-sdi/rashid/security/advisories/new).
Do not open a public issue for security problems.

You can expect an acknowledgment within 7 days and a fix or mitigation plan
within 30 days for confirmed issues.

## Scope

rashid reads and validates catalog metadata (JSON) from local disk. It makes no
network requests and executes no code from the catalogs it validates. Reports
about crashes or resource exhaustion triggered by malicious catalog files are
in scope.

## Automated auditing

- `pip-audit` runs in CI on every push and nightly (`security-audit.yml`),
  with auto-expiring ignores tracked in `.pip-audit-ignores`.
- `bandit` scans the source tree on every push.
- Dependabot monitors GitHub Actions and Python dependencies weekly.
