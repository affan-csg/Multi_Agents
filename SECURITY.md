# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please email `justkhan010@gmail.com` with:

1. A description of the vulnerability
2. Steps to reproduce (if applicable)
3. Potential impact
4. Suggested fix (if you have one)

**Please do not open a public GitHub issue for security vulnerabilities.** We will acknowledge receipt within 48 hours and work with you to understand and address the issue promptly.

## Security Considerations

### Environment Variables

- **Never commit `.env` files.** Use `.env.example` as a template.
- API keys, Supabase service keys, SMTP passwords, and other secrets must be kept private.
- Rotate API keys regularly, especially after deployment or team member departures.
- Use scoped/minimal-privilege API keys whenever possible.

### Database Security

- Both agents use Supabase with PostgREST. Ensure Row-Level Security (RLS) policies are properly configured.
- Service-role keys used by the backend have full database access — treat them as highly sensitive.
- Never use service-role keys in client-side code; use anon keys or auth tokens instead.

### Email / SMTP

- SMTP credentials should use app-specific passwords or API tokens, not personal account passwords.
- Enable TLS/SSL for all SMTP connections (port 587 or 465, not 25).
- Be careful with email content — do not send sensitive data (passwords, private API keys, etc.) in drafts or templates.

### Third-Party APIs

- Gemini, Anthropic, Groq, Tavily, Hunter, Calendly — all have authentication tokens.
- Rotate or revoke API keys if a developer leaves the team.
- Monitor API usage for unusual activity.
- Respect rate limits to avoid account lockouts.

### Dashboard authentication (Server)

- `Server/app.py` guards every HTTP and WebSocket route with `DASHBOARD_PASSWORD`. The Dashboard exchanges the password at `POST /auth/login` for a stateless HMAC-SHA256 token and sends it as `Authorization: Bearer <token>` (or `?token=` on WebSocket handshakes and download links, so the token can appear in access logs — keep logs private).
- It fails closed: with `DASHBOARD_PASSWORD` unset, protected routes return 503. There is a single shared password (no per-user accounts, no rotation other than changing the variable, which invalidates all tokens).
- Exemptions: `/auth/login`, the Calendly webhooks, CORS preflights, and loopback calls from `127.0.0.1`/`::1` that carry no `Origin` header (the agents calling each other). If you deploy behind a reverse proxy on the same host, make sure proxied external traffic does not arrive looking like loopback without an `Origin` header.
- Agents run standalone (without the Server) and the Vercel multi-service deployment have **no** dashboard auth — protect them at the network layer.
- The Calendly webhook verifies `Calendly-Webhook-Signature` with `CALENDLY_WEBHOOK_SIGNING_KEY` (HMAC-SHA256, replay tolerance window) and rejects unsigned calls.

### Frontend (Dashboard)

- The Dashboard is a browser-based SPA. Sensitive operations must happen on the backend; the password is never bundled.
- The login token is kept in `sessionStorage` (cleared when the browser session ends). Never put API keys in the bundle; only `VITE_*` values (the backend origin) are public.
- CORS is restricted: the Server allows `https://multi-agents-beta.vercel.app` and local `localhost`/`127.0.0.1` `:5xxx` dev origins. Add your own deployed Dashboard origin in `Server/app.py` deliberately; don't widen it to `*`.
- Validate all user input on the backend before using it in database queries or external API calls.

### Deployment

- Use HTTPS for all production deployments (no unencrypted HTTP).
- Keep dependencies up-to-date (use Dependabot or similar).
- Regularly audit dependencies for known vulnerabilities: `pip audit`, `npm audit`.
- Configure firewall rules to restrict backend access to intended clients only.
- Monitor logs for unauthorized access attempts.

## Vulnerability Disclosure Timeline

We aim to:
1. Acknowledge receipt within 48 hours
2. Provide a preliminary assessment within 1 week
3. Release a fix or mitigation within 2 weeks (depending on severity)

## Scope

This security policy applies to:
- All code in this repository (Research_Agent, Outreach_Agent, Dashboard, Server)
- Official deployments and infrastructure
- Third-party integrations documented in README files

It does not cover:
- User-deployed instances (your responsibility to secure your own deployment)
- Vulnerabilities in third-party dependencies (report to those projects)
- Social engineering or phishing attacks

## Security Best Practices for Contributors

1. **Code review**: All changes undergo peer review before merging.
2. **Dependency scanning**: Dependabot automatically checks for vulnerable packages.
3. **CI/CD security**: Tests run in isolated environments; no secrets are logged or displayed.
4. **Least privilege**: Each agent uses only the Supabase project and API keys it needs.
5. **Separation of concerns**: Database schemas are separate, minimizing blast radius of a breach.

## Questions?

If you have general security questions (not a vulnerability report), feel free to open a discussion or ask in a PR comment.
