"""Operator insights: onboarding profiles, session/question history and analytics in PostgreSQL.

An add-on to the existing backend that does not change it. `python -m cocoon_agent.insights` serves the
unchanged backend app plus:

- `/v1/insights/*` routes: onboarding (machine choice and employee ID), history, and analytics.
- A capture middleware that records sessions and turns into PostgreSQL after the existing handlers answer.
- `/insights/dashboard`: a small HTML page that charts one employee's analytics.

The existing `python -m cocoon_agent` entrypoint, its SQLite store and its API contract are untouched. If
PostgreSQL is unreachable, the core API keeps working and only the insights routes answer 503.
"""
