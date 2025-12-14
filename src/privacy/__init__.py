"""
Privacy Module for eco-openai-wrapper

DSGVO-compliant PII detection and anonymization using Microsoft Presidio.
Provides transparent middleware for automatic message anonymization/de-anonymization.
"""

from .anonymizer import PresidioAnonymizer, AnonymizationResult
from .middleware import PrivacyMiddleware, get_privacy_middleware

__all__ = [
    'PresidioAnonymizer',
    'AnonymizationResult',
    'PrivacyMiddleware',
    'get_privacy_middleware'
]
