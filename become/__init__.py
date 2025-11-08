"""
Become - A lazy evaluation library for Python with context-aware callable wrappers.

This package provides a simple and elegant way to implement lazy evaluation
and dependency injection patterns in Python applications.

The core concept revolves around 'Be' objects that lazily evaluate their values
only when needed, storing results in a context dictionary for efficient reuse.
"""

from .be import Be, be, be_singleton

__version__ = "0.1.0"
__all__ = ["Be", "be", "be_singleton"]
