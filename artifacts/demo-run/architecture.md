# Architecture

A small service design covering 4 requirements through 7 planned steps.

## Conceptual components

- API layer — accepts long URLs and exposes short-link redirects.
- URL shortening service — creates unique short codes.
- Persistence abstraction — maps short codes to original URLs.
- Redirect handler — resolves known codes and reports unknown ones.

## Design notes

- Keep transport, shortening logic, and storage concerns separate.
- Define the storage boundary now; choose a concrete database later.
- Treat unknown-code behavior as an explicit API contract.
