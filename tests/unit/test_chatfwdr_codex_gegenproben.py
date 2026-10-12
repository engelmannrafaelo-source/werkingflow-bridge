"""CHATFWDR R1 (Codex, 12.10.2026): Gegenproben unveraendert uebernommen aus
local-storage/chatfwdr-r1/test_chatfwdr_review.py — Abnahme fuer P1/P2."""
import asyncio
import json
import time
from types import SimpleNamespace
import pytest
from tests.unit.test_chat_weiterleitung_chatfwd import welt, _post, _abwarten, FRIST_HEADER, POOL_FREI
import src.chat_weiterleitung as cw

@pytest.mark.parametrize('header', ['far_future', 'inf', 'nan'])
def test_external_deadline_cannot_extend_server_limit(welt, monkeypatch, header):
    monkeypatch.setattr(cw, 'CHAT_GESAMTFRIST_S', 1.5)
    value = repr(time.time() + 86400) if header == 'far_future' else header
    # Ordinary client, no relay count: first worker relays after admission refusal.
    response = _post(welt, headers={FRIST_HEADER: value})
    print('EXTERNAL', header, response.status_code, welt.relay_kopf, welt.lauf.ereignisse)
    assert response.status_code != 200, 'external deadline extended 1.5s server budget to permit 3s run'


def test_direct_call_without_header_unchanged(welt):
    # This fixture uses retry-count solely to select its successful admission stub.
    response = _post(welt, headers={'x-bridge-retry-count': '1'})
    assert response.status_code == 200
    assert welt.lauf.ereignisse == ['start', 'ende']


def test_fully_occupied_budget_is_not_account_limit():
    # Real /account-pool-state sets available=false when headroom==0,
    # even with healthy weekly/session quota and zero cooldown.
    accounts = {'private-account-name': dict(worker='worker1', available=False,
        effective_cap_tokens=400000, current_in_flight_tokens=400000,
        cooldown_remaining_s=0, weekly_percent=40)}
    lage = cw.pool_lage(accounts, benoetigt_tokens=163562, ausgeschlossen=set())
    print('FULL', lage)
    assert lage['grund'] == cw.REASON_POOL_KAPAZITAET_BELEGT


def test_reset_of_too_small_worker_is_not_relevant():
    accounts = {
      'small': dict(worker='worker1', available=False, effective_cap_tokens=100,
                    current_in_flight_tokens=0, cooldown_remaining_s=10),
      'large': dict(worker='worker2', available=False, effective_cap_tokens=1000,
                    current_in_flight_tokens=0, cooldown_remaining_s=900)}
    lage = cw.pool_lage(accounts, benoetigt_tokens=500, ausgeschlossen=set())
    print('RESET', lage)
    assert lage['retry_after_s'] == 900


def test_private_account_keys_not_in_public_envelope():
    lage = cw.pool_lage({'private-account-name': next(iter(POOL_FREI.values()))},
                       benoetigt_tokens=10, ausgeschlossen=set())
    request = SimpleNamespace(state=SimpleNamespace(chat_pool_lage=lage))
    assert 'private-account-name' not in json.dumps(cw.ehrliche_lage(request, 'worker busy'))


def test_full_worker_and_limited_peer_retry_when_budget_frees():
    accounts = {
      'busy': dict(worker='worker1', available=False, effective_cap_tokens=400000,
                   current_in_flight_tokens=400000, cooldown_remaining_s=0, weekly_percent=40),
      'limited': dict(worker='worker2', available=False, effective_cap_tokens=400000,
                      current_in_flight_tokens=0, cooldown_remaining_s=7200, weekly_percent=100)}
    lage = cw.pool_lage(accounts, benoetigt_tokens=163562, ausgeschlossen={'worker2'})
    print('MIXED_FULL', lage)
    assert lage['retry_after_s'] == 60


def test_deadline_cancels_admission_queue(welt, monkeypatch):
    import src.main as main
    events = []
    async def queued(request):
        events.append('queued')
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            events.append('cancelled')
            raise
    monkeypatch.setattr(main, 'enforce_pool_admission', queued)
    response = _post(welt, headers={FRIST_HEADER: repr(time.time() + 1.5)})
    assert response.status_code == 504
    assert events == ['queued', 'cancelled']
    assert welt.lauf.ereignisse == []
