# Nakuru Gaz Gang — authentication and deployment security

This build adds customer email/password accounts, email verification, password reset, hashed passwords, secure session cookies, and rate limiting on authentication endpoints.

## Email delivery
Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD and SMTP_FROM in the hosting environment. Without SMTP, verification/reset messages are logged by the application and are not sent to customers. Do not enable SHOW_DEV_EMAIL_LINK on a public deployment.

## Production server
Use Gunicorn rather than Flask's development server. The included Procfile starts Gunicorn. A reverse proxy such as Nginx should sit in front of the application; do not expose Flask port 5000 directly. On managed platforms such as Render, the platform's HTTPS edge/reverse-proxy layer can fill this role.

## Before real launch
Add a full CSRF strategy for browser form/session mutations, persistent PostgreSQL, centralized audit logs, backup/restore testing, administrator MFA (preferably TOTP/security keys), and secrets managed by the hosting provider. Never commit real SMTP credentials or admin passwords to source control.
