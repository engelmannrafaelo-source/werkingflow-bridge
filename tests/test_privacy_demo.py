#!/usr/bin/env python3
"""
Privacy Middleware Demo Test

Demonstrates the anonymization/de-anonymization flow.
Works with or without Presidio installed.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.privacy.anonymizer import PresidioAnonymizer, AnonymizationResult, DetectedEntity


def demo_bypass_anonymization():
    """
    Demo the anonymization logic with sample data (no Presidio required).
    Shows the complete flow: Original → Anonymized → Claude → De-Anonymized
    """
    print("=" * 70)
    print("PRIVACY MIDDLEWARE DEMO - Anonymization Flow")
    print("=" * 70)

    # =========================================================================
    # STEP 1: Original User Message (with PII)
    # =========================================================================
    original_prompt = """
Hallo, ich bin Patrick Pichlbauer von der Getec GmbH.
Meine E-Mail ist p.pichlbauer@getec.at und meine Telefonnummer ist +43 1 234 5678.
Ich wohne in Wien, Österreich.
Bitte senden Sie die Rechnung an unsere Adresse.
"""

    print("\n📥 STEP 1: Original User Message (with PII)")
    print("-" * 50)
    print(original_prompt)

    # =========================================================================
    # STEP 2: Simulated Anonymization (what Presidio would do)
    # =========================================================================
    # Simulate detected entities
    detected_entities = [
        DetectedEntity(
            entity_type="PERSON",
            original_text="Patrick Pichlbauer",
            start=16,
            end=34,
            confidence=0.95,
            placeholder="ANON_PERSON_001"
        ),
        DetectedEntity(
            entity_type="ORGANIZATION",
            original_text="Getec GmbH",
            start=43,
            end=53,
            confidence=0.90,
            placeholder="ANON_ORGANIZATION_001"
        ),
        DetectedEntity(
            entity_type="EMAIL_ADDRESS",
            original_text="p.pichlbauer@getec.at",
            start=71,
            end=92,
            confidence=0.99,
            placeholder="ANON_EMAIL_ADDRESS_001"
        ),
        DetectedEntity(
            entity_type="PHONE_NUMBER",
            original_text="+43 1 234 5678",
            start=120,
            end=134,
            confidence=0.92,
            placeholder="ANON_PHONE_NUMBER_001"
        ),
        DetectedEntity(
            entity_type="LOCATION",
            original_text="Wien",
            start=149,
            end=153,
            confidence=0.88,
            placeholder="ANON_LOCATION_001"
        ),
        DetectedEntity(
            entity_type="LOCATION",
            original_text="Österreich",
            start=155,
            end=165,
            confidence=0.91,
            placeholder="ANON_LOCATION_002"
        ),
    ]

    # Create mapping
    mapping = {e.placeholder: e.original_text for e in detected_entities}

    # Anonymized text (what gets sent to Claude)
    anonymized_prompt = """
Hallo, ich bin ANON_PERSON_001 von der ANON_ORGANIZATION_001.
Meine E-Mail ist ANON_EMAIL_ADDRESS_001 und meine Telefonnummer ist ANON_PHONE_NUMBER_001.
Ich wohne in ANON_LOCATION_001, ANON_LOCATION_002.
Bitte senden Sie die Rechnung an unsere Adresse.
"""

    print("\n🔒 STEP 2: Anonymized Prompt (sent to Claude)")
    print("-" * 50)
    print(anonymized_prompt)

    print("\n📋 Mapping (stored for de-anonymization):")
    print("-" * 50)
    for placeholder, original in mapping.items():
        print(f"  {placeholder} → '{original}'")

    # =========================================================================
    # STEP 3: Claude's Response (with anonymized placeholders)
    # =========================================================================
    claude_response_anonymized = """
Sehr geehrter ANON_PERSON_001,

vielen Dank für Ihre Anfrage im Namen der ANON_ORGANIZATION_001.

Ich habe Ihre Kontaktdaten notiert:
- E-Mail: ANON_EMAIL_ADDRESS_001
- Telefon: ANON_PHONE_NUMBER_001
- Standort: ANON_LOCATION_001, ANON_LOCATION_002

Die Rechnung wird in Kürze an die von Ihnen genannte Adresse versendet.

Mit freundlichen Grüßen,
Ihr Assistent
"""

    print("\n🤖 STEP 3: Claude's Response (with anonymized placeholders)")
    print("-" * 50)
    print(claude_response_anonymized)

    # =========================================================================
    # STEP 4: De-Anonymized Response (returned to user)
    # =========================================================================
    # Simulate de-anonymization
    deanonymized_response = claude_response_anonymized
    sorted_placeholders = sorted(mapping.keys(), key=len, reverse=True)
    for placeholder in sorted_placeholders:
        original = mapping[placeholder]
        deanonymized_response = deanonymized_response.replace(placeholder, original)

    print("\n✅ STEP 4: De-Anonymized Response (returned to user)")
    print("-" * 50)
    print(deanonymized_response)

    # =========================================================================
    # Summary
    # =========================================================================
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"""
🔐 Privacy Protection Flow:

1. User sends message with PII (names, emails, phone numbers, etc.)
2. Middleware ANONYMIZES before sending to Claude:
   - "Patrick Pichlbauer" → "ANON_PERSON_001"
   - "p.pichlbauer@getec.at" → "ANON_EMAIL_ADDRESS_001"
   - "+43 1 234 5678" → "ANON_PHONE_NUMBER_001"

3. Claude processes anonymized content
   - Claude never sees real PII
   - Responses reference placeholders

4. Middleware DE-ANONYMIZES response:
   - "ANON_PERSON_001" → "Patrick Pichlbauer"
   - Restores original PII for user

📊 Statistics:
   - Entities detected: {len(detected_entities)}
   - Entity types: {set(e.entity_type for e in detected_entities)}

🛡️ DSGVO Compliance:
   - PII never leaves local system unprotected
   - Claude only sees pseudonymized data
   - Mapping stored locally, not transmitted
""")


def test_real_presidio():
    """Test with real Presidio if available."""
    print("\n" + "=" * 70)
    print("TESTING REAL PRESIDIO (if available)")
    print("=" * 70)

    anonymizer = PresidioAnonymizer(language='de')

    if not anonymizer.is_available:
        print("\n⚠️  Presidio not installed. Install with:")
        print("   poetry install --extras 'privacy'")
        print("   python -m spacy download de_core_news_lg")
        print("   python -m spacy download en_core_web_lg")
        print("\nNote: Requires Python <3.13 due to srsly compatibility issues.")
        return False

    # Test text
    test_text = "Patrick Pichlbauer von Getec GmbH, E-Mail: p.pichlbauer@getec.at, Tel: +43 1 234 5678"

    print(f"\n📥 Input: {test_text}")

    result = anonymizer.anonymize(test_text)

    print(f"\n🔒 Anonymized: {result.anonymized_text}")
    print(f"\n📋 Mapping: {result.mapping}")
    print(f"\n📊 Entities: {result.entity_count}")

    # De-anonymize
    restored = anonymizer.deanonymize(result.anonymized_text, result.mapping)
    print(f"\n✅ Restored: {restored}")

    return True


if __name__ == "__main__":
    # Always run sample demo
    demo_bypass_anonymization()

    # Try real Presidio test
    test_real_presidio()
