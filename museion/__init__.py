"""Museion: an offline knowledge library with a local AI assistant.

It uses the standard library only, so it runs on the portable Python carried
on the drive with nothing to install, and a person with no internet can read
and change every line of it.

Museion was first written in Rust and TypeScript. Each module here kept the
name of the Rust module it replaced ("sidecar.rs" became sidecar.py), and
comments that say what "the Rust version" did explain behaviour carried over
from it on purpose: indexes and settings it wrote still work.
"""
