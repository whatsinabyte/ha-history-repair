# Security Policy

## Supported versions

Only the latest release is actively maintained and receives security fixes.

| Version | Supported |
|---|---|
| 0.1.x | ✅ Yes |

## Reporting a vulnerability

If you discover a security vulnerability in this app, please report it
responsibly rather than opening a public GitHub issue.

**To report a vulnerability:**

1. Open a [GitHub Security Advisory](../../security/advisories/new) in this
   repository — this keeps the details private until a fix is available
2. Describe the vulnerability, the potential impact, and steps to reproduce it
3. You will receive a response within 7 days

**Please do not:**
- Open a public issue describing a security vulnerability
- Share the details publicly before a fix has been released

## Scope

This app writes directly to your Home Assistant recorder database. Every
change is captured in its own audit table first and is reversible, but the
app's whole reason to exist is to rewrite history your other tools trust —
so its own security posture matters more than most add-ons'.

Security considerations specific to this app:

- **No LAN port.** The app is served exclusively through Supervisor
  **Ingress**: Home Assistant's own login gates every request, and no port
  is published on your network. Anything that could reach this app directly
  could rewrite your recorder database, which is why that path does not
  exist.
- **Database credentials** — declared as `password` in the add-on options
  schema, so Supervisor stores them encrypted and masks them in the UI. They
  are never rendered back by this app.
- **The SQLite filesystem grant is wide by design, not by oversight.**
  SQLite is a single file, not a server this app can connect to over the
  network, and Supervisor has no way to mount just that one file — reaching
  `home-assistant_v2.db` means mounting your entire Home Assistant
  configuration directory (automations, `secrets.yaml`, every other
  integration's config). This grant is declared unconditionally, because
  Supervisor's `map:` list is fixed at install time and cannot depend on
  which `db_type` you choose. In `mariadb`/`postgres` mode the app never
  touches anything outside its own database connection, but the grant is
  still present; see [DOCS.md](DOCS.md#security) for the full reasoning, and
  [ARCHITECTURE.md](ARCHITECTURE.md) for what the AppArmor profile
  (`apparmor.txt`) actually declares.
- **AppArmor runs in `complain` mode, not `enforce`**, meaning violations of
  the declared profile are logged, not blocked. This is a deliberate,
  temporary choice pending real-install log review across all three
  database backends — not yet a hardened boundary. Track this in
  [ARCHITECTURE.md](ARCHITECTURE.md) before assuming the profile is load-bearing.
- **Optional service discovery** (`services: mysql:want, mqtt:want` in
  `config.yaml`) reads connection details from whichever add-on Supervisor
  reports is providing those services. This uses Supervisor's own
  `/services/*` API, exempt from the broader `hassio_api` permission most
  other endpoints require, and grants no access beyond what those services
  themselves already expose.
- **Attribution** — every correction records the Home Assistant user
  responsible, taken from the `X-Remote-User-Display-Name` header Ingress
  supplies. Outside Ingress (local development only), this falls back to
  `"unknown"`.
- **Container installs (`docker-compose.yml`)** have no Ingress and
  therefore no built-in authentication — the port binds to `127.0.0.1` only
  by default, and corrections are attributed to `"unknown"`. Read the
  comments at the top of that file before changing the port binding.

## Acknowledgements

Responsible disclosure of security vulnerabilities is appreciated and
contributors will be credited in the release notes.
