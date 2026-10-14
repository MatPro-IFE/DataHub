# Security assessment of DataHub 1.0.0

Date: October 2026. Scope: the code in this repository (dashboard, ml-api, Python package, database schema and migrations, Compose configuration) as deployed by `docker/docker-compose.yml`. Out of scope: the host operating system, the reverse proxy and the third-party images (PostgreSQL, MinIO, Neo4j), which are kept up to date by their maintainers.

## Method

1. **Threat model.** Assets: raw research data, metadata, user accounts, sharing links and the audit log. Actors: anonymous internet clients, holders of a sharing link, authenticated viewers and uploaders (possibly from another organisation), group managers, administrators, and a compromised process on the internal container network.
2. **Static analysis.** `bandit` over all Python code; manual review of every SQL and Cypher statement, every template, file-path handling, deserialisation and the parsers for uploaded files.
3. **Dependency audit.** `pip-audit` over the packages actually installed in the dashboard (39) and ml-api (52) images.
4. **Dynamic tests.** Probes against a running instance built from the repository: redirects, cookies and sessions, forced password change, cross-site requests, internal API access, path traversal, header injection, crafted XDMF files (path traversal, entity expansion) and log contents.

## Findings and remediation

Severity follows a qualitative scale (likelihood and impact in a typical project deployment). All findings were fixed in the same release and are covered by the end-to-end test (`examples/smoke_test.py`, section 9) or by unit tests (`tests/test_readers.py`), except F16, which is accepted.

| ID | Finding | Severity | Remediation |
|---|---|---|---|
| F1 | Open redirect after login (`next=//other.host`) | Medium | Only local paths accepted; `//` and `/\` refused |
| F2 | Sessions could not be revoked: a copied cookie stayed valid after logout; password changes did not end other sessions | Medium | Cookie bound to a server-side session row checked on every request; logout, password change, password reset and deactivation close sessions; cookie content rotated at login |
| F3 | Forced password change only enforced by a redirect; temporary passwords usable on the API | Medium | Enforced in the authentication dependency: until changed, only the password-change endpoints answer |
| F4 | XDMF reader followed heavy-data paths outside the dataset folder (local file read in the ml-api) | Medium | Referenced files must resolve inside the dataset folder |
| F5 | PCA state stored with `pickle`; write access to the object store would allow code execution in the ml-api | Medium | Stored as `.npz` arrays and loaded with `allow_pickle=False` |
| F6 | ml-api without authentication; any process on the internal network could bypass access control | Medium | Shared bearer token (`ML_API_TOKEN`, required), compared in constant time |
| F7 | Application containers ran as root with all capabilities while parsing untrusted files | Medium | Unprivileged user (uid 10001), read-only root file system, all capabilities dropped, `no-new-privileges` |
| F8 | Sharing-link tokens written to the access log | Medium | Tokens redacted from access-log records |
| F9 | API documentation and OpenAPI schema public | Low | Served only to logged-in users |
| F10 | Session cookie not marked `Secure`; no HSTS | Low | `DATAHUB_COOKIE_SECURE=true` sets both (for HTTPS deployments) |
| F11 | No size limit for uploads (staging disk exhaustion) | Low | Per-upload limits on bytes and number of files |
| F12 | File names inserted unescaped into `Content-Disposition` | Low | RFC 6266 encoding with an ASCII fallback |
| F13 | Front-end libraries and fonts loaded from public CDNs without integrity checks; visitor addresses disclosed to a font provider | Low | All assets served by the dashboard; Content-Security-Policy allows no third-party origin |
| F14 | XML parsed with the standard library | Low | `defusedxml`; entity expansion refused (unit test) |
| F15 | Login time revealed whether a user name exists | Low | Password check against a dummy hash for unknown users |
| F16 | Rate limits for logins and invalid link tokens kept in process memory, per worker | Low | Accepted; documented. Use the reverse proxy's rate limiting for internet-facing instances |
| F17 | Unpinned dependencies; no security scanning in CI | Low | `bandit`, `pip-audit` and `reuse lint` in CI; Dependabot for Python, Docker and Actions |
| F18 | Minimum password length 8 | Low | 12 by default (`DATAHUB_MIN_PASSWORD_LENGTH`) |
| F19 | Cross-site request forgery prevented only by `SameSite=Lax` cookies | Low | Additional same-origin check on all state-changing requests |

## Verified without findings

- No SQL or Cypher injection: user input is always passed as bound parameters; the only formatted SQL fragments are constants (bandit B608 warnings reviewed and annotated).
- Path traversal in uploads (`../`, absolute paths) is refused; object keys are derived from the sanitised path.
- Jinja2 auto-escaping is on; client-side rendering escapes all values.
- The anonymous database role has no privileges; row-level security is enabled on all tables.
- The object-store bucket is private; files reach users only through access-checked endpoints.
- Organisation, group and sharing-link checks hold on all data endpoints (smoke test sections 7 and 8).
- No known vulnerabilities in the installed Python packages at the time of the assessment.

## Remaining risks and recommendations

- Templates use inline scripts, so the Content-Security-Policy contains `'unsafe-inline'` for scripts. Moving scripts to static files would allow a stricter policy.
- Parsers for scientific formats (VTK, HDF5, netCDF) are large native libraries. Their flaws are contained by the container hardening (F7) but not eliminated; keep the images up to date.
- Group managers can add any user to their group and thereby grant access to the group's datasets. Appoint managers accordingly.
- A sharing link is a bearer credential: whoever has the URL has access until it expires or is revoked. Use short lifetimes and download limits.
- Authentication is local (username and password). Single sign-on through an institutional identity provider and two-factor authentication are not yet supported.
- Back-ups contain password hashes and the audit log; protect them like the live system.
