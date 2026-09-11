import pytest

from test_clean_start import setup_clean
from test_shared_billing import A,B,add_event,close_samples
from quota_guard.shared_policy import load_rules
from quota_guard.shared_view import shared_usage,shared_overview
from quota_guard.web_controller import _view


def test_unknown_model_keeps_historical_balance_labelled_without_fabricating_live_allocation(tmp_path):
    db,journals,_=setup_clean(tmp_path)
    journals['one'].append(A,'quota',dict(account=A,used=100,reset_at=800,at=200),200)
    add_event(journals,account=B,at=210,model='codex-auto-review')
    journals['one'].append(B,'quota',dict(account=B,used=3,reset_at=850,at=230),230)
    close_samples(journals)
    labels={A:'账号1',B:'账号2'}
    data=shared_usage(db,'group:test',labels,400,rules=load_rules(db,[A,B],400))
    assert data['billing']['status']=='syncing'
    assert all(p['available'] is None for p in data['billing']['people'].values())
    previous=data['billing']['last_confirmed']
    assert previous['pending_quota']==1
    assert previous['at']<230
    assert [p['available']/p['available_cap']*100 for p in previous['people'].values()]==pytest.approx([100,100,94])
    assert sum(r['tokens'] for r in data['windows']['total']['rows'])==1100
    view=shared_overview(db,'group:test',labels,{},'one',400,data)
    safe=_view(view)
    assert safe['billing']['last_confirmed']['pending_quota']==1
    assert safe['summary']['devices'][0]['confirmed_available']==pytest.approx(100/3)
    assert safe['summary']['devices'][0]['available'] is None
