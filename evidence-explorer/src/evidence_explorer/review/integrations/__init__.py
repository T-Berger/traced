"""Compose specialist handoffs with the existing evidence review application.

Adapters translate saved sources and specialist results into EvidenceBundle.
Workspace services, the combined worker, and UI mounts reuse the review workflow
and Explorer infrastructure. Package/media helpers own bounded local artifact I/O;
specialist algorithms and their original stores remain owned by their packages.
"""
