"""NASA NTRS download (SSRF-hardened) + LangChain PDF loaders.

Skeleton stub. The hardcoded ``ntrs.nasa.gov`` allow-list, https-only egress,
no cross-host redirect following, private-IP/loopback rejection, and per-file
size / total-time limits are implemented in M3.1. PDF loading + normalization
are implemented in M3.2. URLs are compile-time curated only — never taken from
tool arguments or runtime config. See tasks/PLAN.md §5.1, §8.
"""
