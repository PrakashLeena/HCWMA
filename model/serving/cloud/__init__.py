"""Optional cloud LLM (owner decision 2026-11-26). Local only is the default.

Only the owner turns it on, with their own API key (kept in the OS credential store).
Plain HTTPS with httpx; no cloud SDKs. See `dispatcher.py` for how a call is routed.
"""
