"""V5 route policy regressions; no live credentials or provider calls."""
from dataclasses import replace

import pytest

from app.routing.catalog import RouteCatalog, RouteEntry
from app.routing.route_resolver import RouteResolutionError, RouteResolver
from tests.test_control_plane_v2 import DummyFiles, DummyProviders, DummySettings


def _resolver(action):
    high = RouteEntry(provider='openai', model_pattern='gpt-x', connection_id='high',
                      priority=200, offering_id='high', channel_id='channel-high',
                      unavailable_action=action)
    low = RouteEntry(provider='openai', model_pattern='gpt-x', connection_id='low',
                     priority=100, offering_id='low', channel_id='channel-low')
    return RouteResolver(settings=DummySettings(), catalog=RouteCatalog(revision='v5', entries=[high,low]),
                         providers=DummyProviders(), provider_files=DummyFiles())


def test_blocked_top_route_prevents_lower_priority_selection():
    with pytest.raises(RouteResolutionError) as exc:
        _resolver('reject_new_session').resolve(provider='openai', model='gpt-x', purpose='session')
    assert exc.value.code == 'ROUTE_FALLBACK_FORBIDDEN'


def test_caller_allowlist_cannot_bypass_platform_rejection():
    with pytest.raises(RouteResolutionError) as exc:
        _resolver('reject_new_session').resolve(provider='openai', model='gpt-x', purpose='session',
                                                 requirements={'channel_allowlist':['channel-low']})
    assert exc.value.code == 'ROUTE_FALLBACK_FORBIDDEN'


def test_try_next_keeps_existing_priority_behavior():
    binding = _resolver('try_next').resolve(provider='openai', model='gpt-x', purpose='session')
    assert binding.connection_id == 'low'


def test_policy_affects_catalog_hash():
    a = RouteEntry(provider='openai', model_pattern='x', connection_id='x')
    old = RouteCatalog(revision='test', entries=[a])
    changed = RouteCatalog(revision='test', entries=[replace(a, unavailable_action='reject_new_session')])
    assert old.catalog_hash != changed.catalog_hash
