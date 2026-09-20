# Nakuru Gaz Gang

Security-focused Flask web application for Nakuru Gaz Gang: water/gas ordering, delivery services, and technology/network support.

## Production architecture

Customer -> HTTPS/Render edge -> Gunicorn -> Flask -> PostgreSQL

Render's edge provides the public HTTPS/reverse-proxy layer. Flask is not exposed through its development server in production.

## Free Render limitations to plan for

- Free web services sleep after 15 minutes without inbound traffic and may take about a minute to wake.
- The web service filesystem is ephemeral. Do not use SQLite as production storage.
- Free Render Postgres is 1 GB and expires after 30 days; it has no backups. Treat it as a temporary launch database, not permanent business storage.
- Render Free blocks outbound SMTP ports 25/465/587, so email verification/password reset should use an HTTPS email API (for example, Resend) rather than SMTP.

## Security features

- Strong customer passwords with hashing.
- Email verification and password reset tokens stored as SHA-256 hashes and expiring.
- CSRF protection for state-changing requests.
- Secure/HTTP-only/SameSite sessions.
- Rate limiting on authentication and public write endpoints.
- Security headers and restrictive CSP.
- Input length limits and parameterized SQL through SQLAlchemy.
- Indexed frequently queried columns and a batched product lookup for orders.
- Health endpoint at `/healthz`.
- Gunicorn tuned for the 512 MB Free instance: one worker, two threads, keep-alive, bounded request recycling.

## Important

This is a security-focused launch build, not a guarantee of absolute security. Before handling substantial customer/payment volume, add database backups, administrator MFA/TOTP, a migration system, monitoring/alerting, and a controlled security-update pipeline.
