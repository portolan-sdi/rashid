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

rashid reads catalog metadata (JSON) from local disk, and by default reads asset
bytes too. The data pass fetches the `https` hrefs a catalog declares, so
checking a catalog someone handed you issues requests to whatever hosts its
metadata names. `--data-scope local` confines the pass to the catalog tree and
`--no-data` skips it. The live pass (`--live`) and remote schema fetching
(`--schema-allow-network`) are opt-in.

Reading those bytes means parsing untrusted input with GDAL, rasterio, and
pyarrow, so those parsers sit inside the trust boundary. rashid executes no code
from the catalogs it validates. Reports about crashes, resource exhaustion, or
requests a catalog induces to a host the operator did not intend are in scope.

## Automated auditing

- `pip-audit` runs in CI on every push and nightly (`security-audit.yml`),
  with auto-expiring ignores tracked in `.pip-audit-ignores`.
- `bandit` scans the source tree on every push.
- Dependabot monitors GitHub Actions and Python dependencies weekly.
