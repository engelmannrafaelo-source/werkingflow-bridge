"""Compatibility import for the old app-specific research review.

New caller-owned requests do their editorial review in the app.
"""
import sys
from src.compat import research_review

sys.modules[__name__] = research_review
