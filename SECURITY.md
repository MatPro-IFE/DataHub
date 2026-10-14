# Security

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the **Security** tab of the repository and choose **Report a vulnerability**. Do not open a public issue. We aim to respond within ten working days.

## Security model

| Layer | Mechanism |
|---|---|
| Authentication | Username and password (at least 12 characters by default), bcrypt hashes, forced password change for new and reset accounts (enforced on every endpoint), login rate limiting per IP and per user, constant-time response for unknown users |
| Sessions | Signed `HttpOnly`, `SameSite=Lax` cookie (8 h), `Secure` and HSTS with `DATAHUB_COOKIE_SECURE=true`; bound to a server-side session row, so logout, password change, password reset and deactivation end sessions immediately |
| Request integrity | Same-origin check on all state-changing requests; local-only redirects after login; Content-Security-Policy without third-party origins; `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, `Cross-Origin-Opener-Policy` |
| Authorisation | Roles `viewer` (search, download), `uploader` (+ upload, delete own data, sweeps), `admin` (+ users, all data) |
| Dataset access | `allowed_orgs` and `allowed_groups` on every dataset; both empty means visible to all authenticated users; otherwise admins, the uploader and members of a listed organisation or project group; checked on search, detail, download, archive, similarity, graph and sweep endpoints |
| Project groups | Created and deleted by admins; membership changed by admins and group managers; users can only assign groups they belong to; a group that still restricts datasets cannot be deleted |
| Sharing links | Read-only access to one dataset without an account; 256-bit random token, only its SHA-256 stored; mandatory expiry (at most `DATAHUB_SHARE_MAX_DAYS`), optional download limit enforced atomically, revocation; created by the uploader or an admin; file classes hidden from the creator stay hidden; `no-referrer`, `noindex` and `no-store` headers; invalid tokens rate limited per address; every download logged with the link id |
| File filtering | Optional per-organisation hidden file classes (`DATAHUB_ORG_HIDDEN_FILE_CLASSES`), using the class ids of the project schema |
| Audit | `user_sessions` (logins), `download_logs` (every file and archive download with user, file and size) |
| Database | Row-level security enabled on all tables; the `anon` role has no grants; the service role is used only server side |
| Object store | Private bucket; files are streamed by the dashboard after the access check |
| Internal services | ml-api, Neo4j and the optional PostgREST/Studio bind to loopback; the ml-api additionally requires a shared token (`ML_API_TOKEN`) from every caller |
| Containers | Dashboard and ml-api run as uid 10001 on a read-only root file system with all capabilities dropped and `no-new-privileges` |
| Uploaded files | Path traversal refused; per-upload size and file-count limits; XML parsed with `defusedxml`; XDMF data paths confined to the dataset; derived data stored without `pickle` |
| Supply chain | No third-party assets loaded at run time; `bandit`, `pip-audit` and `reuse lint` in CI; Dependabot updates |

## Hardening checklist for a new instance

- [ ] Generate new secrets for every variable in `docker/.env`; never reuse an `.env` from another deployment.
- [ ] Keep `.env` out of version control (it is in `.gitignore`).
- [ ] Keep Neo4j, ml-api, the MinIO console and the optional admin services bound to `127.0.0.1`; use SSH tunnels for administration.
- [ ] Serve the dashboard only through a TLS reverse proxy and set `DATAHUB_COOKIE_SECURE=true` and `DATAHUB_PUBLIC_URL`.
- [ ] Set a long random `ML_API_TOKEN`.
- [ ] Add rate limiting in the reverse proxy for internet-facing instances (the built-in limits are per process).
- [ ] Make sure the reverse proxy does not log full request paths of `/s/...` (sharing-link tokens), or redacts them.
- [ ] Change the bootstrap admin password at first login and create personal accounts for every user.
- [ ] Set `DATAHUB_ORGANIZATIONS` and restrict confidential datasets with organisations or project groups.
- [ ] Decide whether sharing links are allowed and for how long (`DATAHUB_SHARE_MAX_DAYS`, `0` disables them); review active links under Admin, Sharing links.
- [ ] Review the project schema: parameters and metadata fields are visible to every user who can see a dataset.
- [ ] Pin container images by digest for long-running instances and update them regularly.
- [ ] Back up PostgreSQL, the object store and the schema, and test a restore.
- [ ] Start the `admin` profile only when needed and never share the PostgREST service key with end users; it bypasses row-level security and organisation checks.

## Assessment

The results of the security assessment of version 1.0.0, with all findings and their remediation, are in [docs/SECURITY_ASSESSMENT.md](docs/SECURITY_ASSESSMENT.md).
