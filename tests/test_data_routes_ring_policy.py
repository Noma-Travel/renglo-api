"""
Contract tests for the ring policy on /_data.

This is the safety net for a deletion: ~250 lines of product-specific rules
(payment authorization, card redaction, encrypted sidecar, trip approval-field
stripping) came out of data_routes.py and were replaced by a generic policy that
refuses the rings a product API now owns.

What matters here is not that the policy exists but that it fails in the safe
direction. A ring marked `deny` must be refused on every verb, including reads —
the redaction that used to protect reads is gone, so a read that gets through is
a leak, not a degraded response.

The routes are reached through `__wrapped__`, which is the undecorated view
underneath `@cognito_auth_required`. Authentication is not what these tests are
about, and the policy check sits inside the view, after the decorator.
"""

from __future__ import annotations

import json

import pytest
from flask import Flask

from renglo_api.routes import data_routes


PF = 'pf1'
ORG = 'org1'


class FakeDAC:
    """Records what reached the data layer. Any call here means the policy let it through."""

    def __init__(self):
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name, args))

    def get_a_b(self, portfolio, org, ring, limit=None, lastkey=None, sort=None):
        self._record('get_a_b', ring)
        return {'success': True, 'items': [{'_id': 'doc-1'}]}

    def get_a_b_c(self, portfolio, org, ring, idx):
        self._record('get_a_b_c', ring)
        return {'_id': idx, 'ring': ring}

    def get_a_b_query(self, query):
        self._record('get_a_b_query', query.get('ring'))
        return {'success': True, 'items': []}

    def post_a_b(self, portfolio, org, ring, payload):
        self._record('post_a_b', ring)
        return {'success': True, 'item': {'_id': 'new'}}, 200

    def put_a_b_c(self, portfolio, org, ring, idx, payload):
        self._record('put_a_b_c', ring)
        return {'success': True}, 200

    def delete_a_b_c(self, portfolio, org, ring, idx):
        self._record('delete_a_b_c', ring)
        return {'success': True}, 200

    def invalidate_s3_cache(self, portfolio, org, ring, sort=None):
        self._record('invalidate_s3_cache', ring)
        return True

    def refresh_s3_cache(self, portfolio, org, ring, sort=None):
        self._record('refresh_s3_cache', ring)
        return {'success': True, 'items': []}, 200


POLICY = 'ring_closed:deny,ring_ro:deny_write'


@pytest.fixture
def app():
    application = Flask(__name__)
    # _get_renglo_config() prefers app.renglo_config over app.config.
    application.renglo_config = {'DATA_API_RING_POLICY': POLICY}
    return application


@pytest.fixture
def dac(monkeypatch):
    fake = FakeDAC()
    monkeypatch.setattr(data_routes, 'DAC', fake)
    return fake


@pytest.fixture(autouse=True)
def clear_policy_cache():
    """The parse is memoized on the raw string; tests set different ones."""
    data_routes._policy_cache.clear()
    yield
    data_routes._policy_cache.clear()


def status_of(result):
    """Routes return (body, status) or (Response, status)."""
    return result[1] if isinstance(result, tuple) else 200


def body_of(result):
    payload = result[0] if isinstance(result, tuple) else result
    get_data = getattr(payload, 'get_data', None)
    if get_data is not None:
        return json.loads(get_data())
    return payload


# The six verbs, each as a callable taking the ring.
def read_list(ring):
    return data_routes.route_a_b_get.__wrapped__(PF, ORG, ring)


def read_one(ring):
    return data_routes.route_a_b_c_get.__wrapped__(PF, ORG, ring, 'doc-1')


def read_query(ring):
    return data_routes.route_a_b_query.__wrapped__(PF, ORG, ring)


def write_post(ring):
    return data_routes.route_a_b_post.__wrapped__(PF, ORG, ring)


def write_put(ring):
    return data_routes.route_a_b_c_put.__wrapped__(PF, ORG, ring, 'doc-1')


def write_delete(ring):
    return data_routes.route_a_b_c_delete.__wrapped__(PF, ORG, ring, 'doc-1')


READS = [('list', read_list), ('one', read_one), ('query', read_query)]
WRITES = [('post', write_post), ('put', write_put), ('delete', write_delete)]


def invoke(app, view, ring, *, method='GET'):
    # The list route has two branches: an S3 snapshot one (the default) and a
    # DynamoDB one. `paged=1` picks the DynamoDB branch, which is the one that
    # goes through the fake — the S3 branch would reach for a real bucket.
    path = '/?paged=1' if method == 'GET' else '/'
    with app.test_request_context(path, method=method, json={}):
        return view(ring)


def method_for(label):
    return 'GET' if label in ('list', 'one') else 'POST'


# ── deny: nothing gets through, reads included ───────────────────────────────

@pytest.mark.parametrize('label,view', READS + WRITES)
def test_deny_refuses_every_verb(app, dac, label, view):
    result = invoke(app, view, 'ring_closed', method=method_for(label))
    assert status_of(result) == 403, f'{label} was allowed on a denied ring'
    assert dac.calls == [], f'{label} reached the data layer on a denied ring'


def test_deny_message_names_the_ring(app, dac):
    result = invoke(app, read_list, 'ring_closed')
    body = body_of(result)
    assert body['success'] is False
    assert 'ring_closed' in body['message']


# ── deny_write: reads survive on purpose ─────────────────────────────────────

@pytest.mark.parametrize('label,view', WRITES)
def test_deny_write_refuses_writes(app, dac, label, view):
    result = invoke(app, view, 'ring_ro', method='POST')
    assert status_of(result) == 403
    assert dac.calls == []


@pytest.mark.parametrize('label,view', READS)
def test_deny_write_still_reads(app, dac, label, view):
    """The rule that was removed for this ring was a write rule. Reads must not regress."""
    result = invoke(app, view, 'ring_ro', method=method_for(label))
    assert status_of(result) == 200
    assert dac.calls, f'{label} was blocked on a deny_write ring'


# ── rings outside the policy, and no policy at all ───────────────────────────

@pytest.mark.parametrize('label,view', READS + WRITES)
def test_unlisted_ring_passes(app, dac, label, view):
    result = invoke(app, view, 'ring_other', method=method_for(label))
    assert status_of(result) == 200
    assert dac.calls


@pytest.mark.parametrize('label,view', READS + WRITES)
def test_no_policy_means_generic_crud(app, dac, label, view):
    app.renglo_config['DATA_API_RING_POLICY'] = ''
    result = invoke(app, view, 'ring_closed', method=method_for(label))
    assert status_of(result) == 200
    assert dac.calls


def test_missing_key_means_generic_crud(app, dac):
    del app.renglo_config['DATA_API_RING_POLICY']
    result = invoke(app, read_list, 'ring_closed')
    assert status_of(result) == 200


# ── a typo in an env var must not take the API down ──────────────────────────

@pytest.mark.parametrize('raw', [
    'garbage',                       # no colon at all
    'ring_closed',                   # ring with no mode
    ':deny',                         # mode with no ring
    'ring_closed:banana',            # unknown mode
    ',,ring_closed:deny,,',          # empty entries around a valid one
    'ring_closed : DENY ',           # whitespace and case
])
def test_malformed_policy_does_not_raise(app, dac, raw):
    app.renglo_config['DATA_API_RING_POLICY'] = raw
    result = invoke(app, read_list, 'ring_closed')
    assert status_of(result) in (200, 403)


def test_whitespace_and_case_are_tolerated(app, dac):
    app.renglo_config['DATA_API_RING_POLICY'] = ' ring_closed : DENY '
    result = invoke(app, read_list, 'ring_closed')
    assert status_of(result) == 403


def test_unknown_mode_is_ignored_not_treated_as_deny(app, dac):
    """Fail-open on a typo'd mode is deliberate: the alternative is an outage on a typo."""
    app.renglo_config['DATA_API_RING_POLICY'] = 'ring_closed:banana'
    result = invoke(app, read_list, 'ring_closed')
    assert status_of(result) == 200


# ── the parse is memoized ────────────────────────────────────────────────────

def test_policy_is_parsed_once_per_string(app, dac):
    assert data_routes._policy_cache == {}
    invoke(app, read_list, 'ring_other')
    invoke(app, read_list, 'ring_other')
    invoke(app, read_list, 'ring_other')
    assert list(data_routes._policy_cache) == [POLICY]


def test_changing_the_config_takes_effect(app, dac):
    """Memoization keys on the raw string, so a new value must not serve a stale policy."""
    assert status_of(invoke(app, write_post, 'ring_closed', method='POST')) == 403
    app.renglo_config['DATA_API_RING_POLICY'] = 'other:deny'
    assert status_of(invoke(app, write_post, 'ring_closed', method='POST')) == 200


# ── the /_all write path is covered too ──────────────────────────────────────

def test_all_org_post_respects_the_policy(app, dac):
    with app.test_request_context('/', method='POST', json={}):
        result = data_routes.route_a_all_post.__wrapped__(PF, 'ring_closed')
    assert status_of(result) == 403
    assert dac.calls == []
