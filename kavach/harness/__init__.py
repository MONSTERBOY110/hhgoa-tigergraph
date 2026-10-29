"""Harness: the layer that makes an investigation safe to run inside a bank.

- faults.py     typed failures (ToolFailure, ModelFailure, BoundsExceeded, Forbidden) and KAVACH_FAULT injection
- bounds.py     hard limits per case (tool calls, LLM writes, seconds per tool call, seconds per case)
- redact.py     masks secrets, bearer tokens and email local parts in anything we log or persist
- approvals.py  server-side role checks and an append-only, hash-chained approval log
- runner.py     run_case(): fail closed, write an incident instead of an answer when anything breaks

Nothing here makes a decision about a case; it only decides whether a result may be trusted and kept.
"""
