# Curated Sample Output

This Git-tracked directory contains curated reference copies, not live execution
history or a runtime destination:

- `url-shortener-v17/` is a representative governed greenfield publication.
- `url-shortener-v18-expiration/` is the governed brownfield evolution of V17.
  V17 remains preserved unchanged; V18's evidence records V17 as its baseline.
- `reliability_metrics.json` is a deterministic reliability projection over these
  two evidence bundles.
- `workflow_diagram.png` is the current orchestration graph retained with both
  governed runs.

Each copied `sdlc-artifacts/` directory preserves the real governed evidence from
its successful publication. The authoritative original histories remain under
`runs/<run-id>/sdlc-artifacts/`, and the durable source publications remain under
`projects/<project-name>/` with their manifest-verified evidence copies. Normal
CLI and Streamlit execution must never write to `sample_output/`.

Requirement Analysis still exposes ambiguities, blocking/non-blocking readiness,
human review, and clarified authority revisions. That governance is demonstrated
interactively in the live workflow rather than through a third frozen sample
folder.
