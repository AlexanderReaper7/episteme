"""Recommendation subsystem (spec §8): the canonical topic vocabulary, the
interest profile derived from the feedback log, and the scorers that rank the
feed. Kept out of `worker/` because the web layer reads the same code paths —
the worker only owns the jobs that call into it.
"""
