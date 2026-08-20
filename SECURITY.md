# Security Policy

PalServer Manager controls game-server processes, files, backups and player administration. Treat access to the manager agent as equivalent to administrator access to the Palworld server.

## Remote-agent rules

- Keep the agent on `127.0.0.1` whenever possible.
- Prefer SSH or a private VPN for off-network management.
- Never expose Palworld's built-in REST API directly to the public Internet.
- Direct manager-agent WAN mode requires TLS and is disabled by default.
- Use a long generated manager token and rotate it if it is disclosed.
- Do not commit `config.json`, private keys, server passwords or tokens.

## Reporting a vulnerability

Before public release, replace this section with a private security contact or enable GitHub Private Vulnerability Reporting. Do not ask reporters to publish working exploits in a public issue.
