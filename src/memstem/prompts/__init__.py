"""Prompt templates loaded by the hygiene workers.

These live alongside the source so they ship with the installed package
and ``importlib.resources`` can read them. Tests pass mock judges and
never load the templates from disk.
"""
