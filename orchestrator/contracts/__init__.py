"""What's left of Member C's local stand-ins, now that B's `common`/`firewall`/
`validator` and A's `sim` are real: only `sim_client.py`, the HTTP client for
A's insurer simulator. It always tries the real `/submit` and `/appeal`
endpoints first and only falls back to a local generator if the simulator
isn't reachable -- so it's still useful standalone, but everything else
(models, rules, search, firewall, validator, profiles, llm) now comes
directly from `common`/`firewall`/`validator`.
"""
