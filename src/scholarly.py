"""Legacy OA editorial enrichment; new callers provide their own material.

Delete this adapter with compat after legacy research clients migrate.
"""
import sys
from src.compat import scholarly as _legacy
sys.modules[__name__] = _legacy
