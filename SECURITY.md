# Security policy

Security fixes target the latest code on `main`. Ruminate is currently a 0.x
project; older versions do not have separate maintenance branches.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/hanikasarfa94-max/Ruminate/security/advisories/new).
Include the affected version, a minimal reproduction, expected behavior and
potential impact. Synthetic examples are preferred; omit credentials and private
research material.

If private reporting is unavailable, open an issue asking for a private contact
route without publishing exploit details or affected data. There is no guaranteed
response time; reports are reviewed by the project maintainers.

For ordinary bugs and usage questions, see [SUPPORT.md](SUPPORT.md).

## Data and execution boundaries

Ruminate stores documents and research locally. Snippets and page images sent
through MCP become part of the connected AI host's context. URL ingestion makes
an explicitly requested network request. Installing dependencies also uses the
configured package registry.

The detailed implementation, resource limits, file scopes, URL checks and current
limitations are described in [docs/SECURITY.md](docs/SECURITY.md).
