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

### Frontend (Dashboard)

- The Dashboard is a browser-based SPA. Sensitive operations must happen on the backend.
- Avoid storing authentication tokens or API keys in `localStorage` (vulnerable to XSS).
- Use CORS carefully — the backend is configured to accept requests from known origins.
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
