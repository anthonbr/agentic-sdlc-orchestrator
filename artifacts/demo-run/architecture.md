# Architecture

A small conceptual service design supporting 6 approved engineering tasks; the tasks are not executed in V0.4.

## Conceptual components

- API layer — accepts long URLs and exposes short-link redirects.
- URL shortening service — creates unique short codes.
- Persistence abstraction — maps short codes to original URLs.
- Redirect handler — resolves known codes and reports unknown ones.

## Design notes

- Keep transport, shortening logic, and storage concerns separate.
- Define the storage boundary now; choose a concrete database later.
- Treat approved ambiguities as unresolved until their linked tasks decide them.
