"""PROPOSED target contract (I01). Nothing in this package is served by the running app.

The runtime v1 contract lives in `cocoon_agent.api.schemas` and is exported to
`contracts/openapi.yaml`. This package defines the *future* interfaces from
BACKEND_IMPLEMENTATION_PLAN.md section 6 so that the voice, Android, supervisor
and data streams can build against a frozen shape. It is exported separately to
`contracts/proposed/` by `scripts/export_proposed_contract.py`.

Import-safety rule: modules here import only pydantic, the standard library and
`cocoon_agent.api.schemas` (for shared primitives). They never import the app,
the store, the graph or settings, and importing them registers no route.
"""
