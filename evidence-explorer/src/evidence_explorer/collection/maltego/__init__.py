"""Maltego Data MCP as a Traced data source.

Lookups are explicit actions against the remote Data MCP (OAuth sign-in with
the person's own Maltego account). Each result is frozen as a hashed record in
the selected workspace and can become a review case through the existing
saved-source import. Collection status, records, model proposals, and human
decisions stay separate; a record asserts only what the Data MCP returned.

Modules: ``settings`` (configuration), ``tokens`` and ``oauth`` (sign-in),
``transport`` (MCP Streamable HTTP client), ``records`` (frozen evidence),
``service`` (explicit operations), and ``cli`` (``./traced maltego``).
"""
