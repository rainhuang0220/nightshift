# Security

Nightshift runs untrusted model output next to a real checkout. Read `docs/threat-model.md` before you trust a control.

The current safety result for the sacrificial Grok canary is `PASS_WITH_LIMITATIONS`. The provider process needs network access to reach the model API, so arbitrary external network mutation is not an operating-system hard block. Filesystem writes outside the seatbelt's writable roots are blocked. Details are in `docs/threat-model.md` and `CHANGELOG.md`.

## Reporting a vulnerability

A private security-reporting channel is not configured yet. That infrastructure is still being established.

Until it exists:

- Do not open a public issue that includes credentials, tokens, session files, or a working exploit against a real repository.
- Do not attach auth files, provider logs, or seatbelt profiles. Describe the affected version, the component, and the impact.
- Prefer to wait for the private channel rather than posting exploit-sensitive detail in public.

Supported versions are the tagged releases on `main`, starting with `v0.2.0`. `v0.1.0` is the historical control-plane milestone and does not include the isolation gate.
