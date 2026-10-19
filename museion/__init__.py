"""Museion: an offline knowledge library with a local AI assistant.

This package is the Python rewrite of the Rust core in crates/museion-core.
It uses the standard library only, so it runs on the portable Python carried
on the drive with nothing to install, and a person with no internet can read
and change every line of it.

Each module keeps the name of the Rust module it replaces, so the comments
and docs that point at "sidecar.rs" or "pdf.rs" still lead to the right
place.
"""
