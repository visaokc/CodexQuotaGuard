import pytest

from quota_guard.limit_controls import limit_presentation, parse_cap
from test_core import A, B
from test_account_scope import setup, ident


@pytest.mark.parametrize('value,expected', [('33', 33), ('33.5%', 33.5), ('0.01', .01), ('100', 100), (' 5.25 ', 5.25)])
def test_cap_parser_valid(value, expected):
    assert parse_cap(value) == expected


@pytest.mark.parametrize('value', ['', 'abc', 'NaN', 'Infinity', '-1', '0', '100.01', '1.234'])
def test_cap_parser_invalid(value):
    with pytest.raises(ValueError):
        parse_cap(value)


def test_limit_states_reflect_runtime_not_just_checkbox():
    view = dict(identity=ident(), auto_block=True, blocked=False, summary=dict(epoch={'used': 20}, reset_pending=False))
    state = limit_presentation(view, {A: {}})
    assert state['key'] == 'on' and state['tone'] == 'active' and state['command'] == 'disable'
    assert limit_presentation(dict(view, auto_block=False), {A: {}})['key'] == 'off'
    assert limit_presentation(dict(view, blocked=True, auto_block=False), {})['key'] == 'blocked'
    assert limit_presentation(dict(view, identity=ident('', 'api')), {A: {}})['key'] == 'inactive'
    assert limit_presentation(dict(view, identity=ident(B)), {A: {}})['key'] == 'inactive'
    assert limit_presentation(dict(view, error='timeout'), {A: {}})['key'] == 'waiting'
    assert limit_presentation(view, {A: {}}, pending=False)['key'] == 'pending'
    assert limit_presentation({}, {A: {}})['key'] == 'unknown'


def test_cap_dialog_account_target_guard(tmp_path):
    engine, current, step, *_ = setup(tmp_path)
    step(100)
    engine.set_cap(45.5, expected_account=A)
    step(110)
    assert engine.ledger.summary(A, 110)['devices'][0]['cap'] == 45.5
    current[0] = ident(B)
    with pytest.raises(ValueError, match='登录账号已变化'):
        engine.set_cap(60, expected_account=A)
    step(120)
    assert engine.ledger.summary(B, 120)['devices'][0]['cap'] == 33
