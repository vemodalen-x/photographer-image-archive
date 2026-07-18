# Security

## Supported version

Security fixes are provided for the latest published release.

## Reporting

Report vulnerabilities through the repository's private GitHub security advisory flow. Do not include personal archives, downloaded photographs, credentials, or private source URLs in a public issue.

The app does not bypass authentication, paywalls, DRM, anti-bot controls, or access restrictions. Dynamic rendering is limited to public pages and runs with an isolated temporary browser profile.

User-controlled HTTP(S) destinations are resolved before connection and loopback, private, link-local, reserved, and otherwise non-public addresses are rejected. Redirects are checked one hop at a time and the connected peer address is verified before response content is consumed. Dynamic Chromium rendering pins the selected official host to a validated public address; all non-allowlisted browser traffic is sent to a local deny-only proxy.
