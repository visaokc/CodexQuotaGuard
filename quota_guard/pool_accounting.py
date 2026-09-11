"""Deterministic integer quota inventory; tokens are never written by this module."""
from decimal import Decimal, ROUND_HALF_UP

from .shared_policy import PERSONS

UNIT = 10**9
FULL = 100*UNIT


def units(value):
    return int((Decimal(str(value))*UNIT).to_integral_value(rounding=ROUND_HALF_UP))


def points(value):
    return value/UNIT


def split(amount, weights):
    """Largest remainders with a stable tie order preserve every quota unit."""
    total = sum(weights.values())
    if amount < 0 or total <= 0:
        if amount == 0:
            return {key: 0 for key in weights}
        raise ValueError('可分配份额不足')
    values = {key: amount*weight//total for key, weight in weights.items()}
    order = sorted(weights, key=lambda key: (-(amount*weights[key] % total), key))
    for key in order[:amount-sum(values.values())]:
        values[key] += 1
    return values


class Pool:
    def __init__(self, accounts, compensation=False, rollover_since=None):
        self.accounts = list(accounts)
        self.enabled = compensation
        self.stock = {a: {p: 0 for p in PERSONS} for a in accounts}
        self.entitlements = {a: {p: 0 for p in PERSONS} for a in accounts}
        self.rollover_since = rollover_since
        self.bank = {p: 0 for p in PERSONS}
        self.remaining = {a: 0 for a in accounts}
        self.pending = {a: {p: 0 for p in PERSONS} for a in accounts}
        self.confirmed = {p: 0 for p in PERSONS}
        self.cycles = {}
        self.entries = []

    def record(self, kind, at, account='', **value):
        self.entries.append(dict(kind=kind, at=at, account=account,
                                 cycle=self.cycles.get(account), **value))

    def grant(self, account, amount, cycle, at, initial=False):
        self.cycles[account] = cycle
        self.remaining[account] = amount
        self.stock[account] = split(amount, {p: 1 for p in PERSONS})
        self.entitlements[account] = dict(self.stock[account])
        self.record('initial' if initial else 'grant', at, account, amount=points(amount))
        if self.enabled and not initial:
            payments = {p: min(self.stock[account][p], max(0, self.confirmed[p])) for p in PERSONS}
            credits = {p: max(0, -self.confirmed[p]) for p in PERSONS}
            total = sum(payments.values())
            received = split(total, credits) if total else {p: 0 for p in PERSONS}
            for p in PERSONS:
                self.stock[account][p] += received[p]-payments[p]
                self.confirmed[p] += received[p]-payments[p]
                if payments[p] or received[p]:
                    self.record('repay', at, account, person=p,
                                paid=points(payments[p]), received=points(received[p]))
        self.check()

    def spend(self, account, person, amount, at):
        if amount < 0 or amount > self.remaining[account]:
            raise ValueError('消费超过已确认的账号余额')
        left = amount
        own = min(left, self.stock[account][person])
        self.stock[account][person] -= own
        left -= own
        for other in self.accounts:
            if other == account or not left:
                continue
            holders = {p: self.stock[account][p] for p in PERSONS if p != person}
            exchange = min(left, self.stock[other][person], sum(holders.values()))
            if not exchange:
                continue
            portions = split(exchange, holders)
            for holder, part in portions.items():
                self.stock[account][holder] -= part
                self.stock[other][holder] += part
            self.stock[other][person] -= exchange
            self.record('exchange', at, account, person=person, other_account=other, amount=points(exchange))
            left -= exchange
        if left and self.bank[person]:
            redeemed = min(left, self.bank[person])
            portions = split(redeemed, {p: self.stock[account][p] for p in PERSONS if p != person})
            # Redeem a saved right against real inventory. Its previous holder
            # receives the saved right, so nobody loses their personal balance.
            for holder, part in portions.items():
                self.stock[account][holder] -= part
                self.bank[holder] += part
            self.bank[person] -= redeemed
            left -= redeemed
            self.record('redeem', at, account, person=person, amount=points(redeemed))
        if left:
            portions = split(left, {p: self.stock[account][p] for p in PERSONS if p != person})
            for holder, part in portions.items():
                self.stock[account][holder] -= part
                if self.enabled:
                    self.pending[account][holder] -= part
            if self.enabled:
                self.pending[account][person] += left
            self.record('borrow' if self.enabled else 'extra', at, account, person=person,
                        amount=points(left), creditors={p: points(v) for p, v in portions.items() if v})
        self.remaining[account] -= amount
        self.record('consume', at, account, person=person, amount=points(amount))
        self.check()

    def reset(self, account, cycle, at, cause, remaining=FULL, exempt=False):
        old = dict(self.pending[account])
        confirm = self.enabled and cause in ('natural', 'card') and not exempt
        if confirm:
            for p in PERSONS:
                self.confirmed[p] += old[p]
        if any(old.values()):
            self.record('confirm' if confirm else 'waive', at, account, reason='exempt' if exempt else cause,
                        balances={p: points(v) for p, v in old.items()})
        self.pending[account] = {p: 0 for p in PERSONS}
        if self.rollover_since is not None and at >= self.rollover_since:
            for p in PERSONS:
                saved = self.stock[account][p]
                self.bank[p] += saved
                if saved:
                    self.record('rollover', at, account, person=p, amount=points(saved))
        self.record('expire', at, account, amount=points(self.remaining[account]))
        self.grant(account, remaining, cycle, at)

    def compensation(self, enabled, at):
        if self.enabled and not enabled:
            self.record('archive', at, balances={p: points(self.debt(p)) for p in PERSONS})
            self.confirmed = {p: 0 for p in PERSONS}
            self.pending = {a: {p: 0 for p in PERSONS} for a in self.accounts}
        self.enabled = enabled

    def debt(self, person):
        return self.confirmed[person]+sum(value[person] for value in self.pending.values())

    def check(self):
        assert sum(self.confirmed.values()) == 0
        assert all(value >= 0 for value in self.bank.values())
        for account in self.accounts:
            assert sum(self.stock[account].values()) == self.remaining[account]
            assert sum(self.pending[account].values()) == 0
            assert all(value >= 0 for value in self.stock[account].values())
            assert 0 <= self.remaining[account] <= FULL

    def summary(self):
        return {p: dict(available=points(sum(self.stock[a][p] for a in self.accounts)+self.bank[p]),
                       rollover=points(self.bank[p]),
                       available_cap=points(sum(self.entitlements[a][p] for a in self.accounts)),
                       fair_usage=points(sum(self.entitlements[a][p]-self.stock[a][p] for a in self.accounts)-self.bank[p]+self.debt(p)),
                       by_account={a: points(self.stock[a][p]) for a in self.accounts},
                       pending=points(sum(self.pending[a][p] for a in self.accounts)),
                       confirmed=points(self.confirmed[p]), debt=points(self.debt(p))) for p in PERSONS}
