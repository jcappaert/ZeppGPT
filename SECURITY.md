# Security policy

## Reporting a vulnerability

Please report security vulnerabilities through GitHub's private vulnerability
reporting feature when it is available for this repository. Do not include a
real Zepp token, user ID, workout payload, route, or diagnostic export in a
public issue.

If private reporting is unavailable, open a public issue containing only a
minimal synthetic reproduction and ask the maintainer for a private contact
channel.

## Account-data safety

- Keep credentials only in the ignored `.env` file.
- Use ZeppGPT only with an account you are authorized to access.
- Treat diagnostic output as sensitive health and location data.
- Revoke or replace a token immediately if it is disclosed.
- Keep the HTTP transport on loopback unless it is protected by an
  authenticated tunnel or proxy.

ZeppGPT is an unofficial integration. Zepp authentication and API behavior may
change without notice.
