"""Replicate explicitly shared account journals without enrolling or billing them."""
import copy
import json
import math
import re

from . import __version__

from .sync_diagnostics import rejection_reason


_DIRECTORY_KEY = 'shared_sync:directory:v1'
_ACCOUNT = re.compile(r'[a-f0-9]{64}')
_MAX_MEMBERS = 16
_MAX_ACCOUNTS = 16
_MAX_ORIGINS = 128
_MAX_BYTES = 160*1024
_INTERVAL = 5


def _account(value):
    if not isinstance(value, str) or not _ACCOUNT.fullmatch(value):
        raise ValueError('共享账号标识无效')
    return value


def _identity(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise ValueError('共享成员标识无效')
    return value


def _accounts(value):
    if not isinstance(value, list) or len(value) > _MAX_ACCOUNTS:
        raise ValueError('共享账号目录无效')
    result = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError('共享账号目录无效')
        account, label = _account(row.get('account')), row.get('label')
        if not isinstance(label, str) or len(label) > 100 or account in result:
            raise ValueError('共享账号目录无效')
        result[account] = label
    return [dict(account=a, label=result[a]) for a in sorted(result)]


def _declaration(value):
    if not isinstance(value, dict):
        raise ValueError('共享成员目录无效')
    device, name = _identity(value.get('device')), value.get('name')
    revision = value.get('revision')
    if (not isinstance(name, str) or not 1 <= len(name) <= 80
            or type(revision) is not int or not 1 <= revision <= 1e9):
        raise ValueError('共享成员目录无效')
    return dict(device=device, name=name, revision=revision, accounts=_accounts(value.get('accounts')))


def _vector(value):
    if not isinstance(value, dict) or len(value) > _MAX_ORIGINS:
        raise ValueError('同步版本向量无效')
    if any(not isinstance(k, str) or not 1 <= len(k) <= 100
           or type(v) is not int or not 0 <= v <= 1e9 for k, v in value.items()):
        raise ValueError('同步版本向量无效')
    return dict(value)


class SharedSync:
    def __init__(self, database, journal, device, name, tracked):
        self.db, self.journal = database, journal
        self.device, self.name, self.tracked = _identity(device), name, tracked
        saved = database.get(_DIRECTORY_KEY, {})
        self.directory = {key: _declaration(value) for key, value in saved.items()}
        if any(key != row['device'] for key, row in self.directory.items()) or len(self.directory) > _MAX_MEMBERS:
            raise ValueError('共享成员目录无效')
        self.vectors, self.receipts, self.errors = {}, {}, {}
        self.catalog_receipts, self.catalog_errors, self._catalog_at = {}, {}, {}
        self.peer_accounts, self.current_accounts = {}, {}
        self.peer_versions = {}
        self._sent, self._last_catalog = {}, {}
        self._presence = None
        self._update_own()

    def _update_own(self):
        old = self.directory.get(self.device)
        # Stopping local collection does not discard a previously shared journal.
        shared = {item['account']: item['label'] for item in old['accounts']} if old else {}
        shared.update({a: p.get('label', a[:8]) for a, p in self.tracked.items()})
        accounts = _accounts([dict(account=a, label=label) for a, label in shared.items()])
        if old and old['name'] == self.name and old['accounts'] == accounts:
            return False
        record = _declaration(dict(device=self.device, name=self.name,
            revision=old['revision']+1 if old else 1, accounts=accounts))
        self.directory[self.device] = record
        if len(self.directory) > _MAX_MEMBERS:
            raise ValueError('共享成员目录过大')
        self.db.put(_DIRECTORY_KEY, self.directory)
        return True

    def _labels(self, directory=None):
        result = {}
        for device, row in sorted((directory or self.directory).items()):
            for item in row['accounts']:
                result.setdefault(item['account'], item['label'])
        if len(result) > _MAX_ACCOUNTS:
            raise ValueError('共享账号目录过大')
        return result

    def _catalog(self, now):
        own = self.directory[self.device]
        message = dict(type='group_sync', at=now, name=own['name'], revision=own['revision'],
                    app_version=__version__,
                    accounts=copy.deepcopy(own['accounts']),
                    directory=[copy.deepcopy(row) for device, row in sorted(self.directory.items()) if device != self.device],
                    current_account=self.current_accounts.get(self.device),
                    vectors={account: self.journal.vector(account) for account in self._labels()},
                    **({'presence': copy.deepcopy(self._presence)} if self._presence else {}))
        if len(json.dumps(message, separators=(',', ':')).encode()) > _MAX_BYTES:
            raise ValueError('共享成员目录过大')
        return message

    def _send_catalog(self, peer, mesh, now):
        mesh.send(peer, self._catalog(now))
        self._last_catalog[peer] = now

    def _allowed(self, peer, account):
        if account not in self.tracked and account not in self.peer_accounts.get(peer, set()):
            raise ValueError('来源尚未声明共享此账号')

    def _presence_value(self, peer, account, value, now):
        if (not isinstance(value, dict) or value.get('device') != peer
                or value.get('account') != account):
            raise ValueError('设备心跳无效')
        for field in ('at', 'scan_at'):
            if type(value.get(field)) not in (int, float) or not math.isfinite(value[field]):
                raise ValueError('设备心跳无效')
        if abs(now-value['at']) > 90:
            raise ValueError('设备心跳无效')
        for field in ('active', 'uncertain', 'unbound_active', 'unbound_uncertain'):
            if field in ('active', 'uncertain') and field not in value:
                raise ValueError('设备心跳无效')
            if type(value.get(field, 0)) is not int or not 0 <= value.get(field, 0) <= 10000:
                raise ValueError('设备心跳无效')
        return value

    def _set_current(self, peer, account, presence, now):
        if peer not in self.current_accounts or self.current_accounts[peer] != account:
            self.journal.ledger.logout(peer)
        self.current_accounts[peer] = account
        if presence:
            self.journal.presence(account, peer, presence, now)

    def _receive_catalog(self, peer, message, mesh, now):
        if len(json.dumps(message, separators=(',', ':')).encode()) > _MAX_BYTES:
            raise ValueError('共享成员目录过大')
        at = message.get('at', now)
        if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at <= now+60:
            raise ValueError('共享目录时间无效')
        if at < self._catalog_at.get(peer, 0):
            return
        version = message.get('app_version', '')
        self.peer_versions[peer] = version if isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+', version) else ''
        direct = _declaration(dict(device=peer, name=message.get('name'),
            revision=message.get('revision', 1), accounts=message.get('accounts')))
        forwarded = message.get('directory', [])
        if not isinstance(forwarded, list) or len(forwarded) > _MAX_MEMBERS:
            raise ValueError('共享成员目录过大')
        records = [_declaration(row) for row in forwarded] + [direct]
        candidate = copy.deepcopy(self.directory)
        for row in records:
            if row['device'] == self.device:
                continue
            old = candidate.get(row['device'])
            if old and old['revision'] == row['revision'] and old != row:
                raise ValueError('共享成员目录版本冲突')
            if old is None or row['revision'] > old['revision']:
                candidate[row['device']] = row
        if len(candidate) > _MAX_MEMBERS:
            raise ValueError('共享成员目录过大')
        labels = self._labels(candidate)
        vectors = message.get('vectors')
        if not isinstance(vectors, dict) or len(vectors) > _MAX_ACCOUNTS:
            raise ValueError('同步版本向量无效')
        vectors = {_account(a): _vector(v) for a, v in vectors.items()}
        if any(account not in labels for account in vectors):
            raise ValueError('来源尚未声明共享此账号')
        current = message.get('current_account')
        own_accounts = {row['account'] for row in direct['accounts']}
        if current is not None and _account(current) not in own_accounts:
            raise ValueError('当前账号尚未由此成员添加')
        presence = message.get('presence')
        if presence is not None:
            if current is None:
                raise ValueError('设备心跳无效')
            self._presence_value(peer, current, presence, now)
        first = peer not in self.catalog_receipts
        changed = candidate != self.directory
        if changed:
            self.directory = candidate
            self.db.put(_DIRECTORY_KEY, candidate)
        self.peer_accounts[peer] = own_accounts | vectors.keys()
        self.catalog_receipts[peer] = now
        self._catalog_at[peer] = at
        self.catalog_errors.pop(peer, None)
        self._set_current(peer, current, presence, now)
        for account, vector in vectors.items():
            self.vectors[account, peer] = vector
            self.receipts[account, peer] = now
        if first or changed:
            self._send_catalog(peer, mesh, now)
        for account in vectors:
            self._push_page(account, peer, mesh, now)

    def _push_page(self, account, peer, mesh, now):
        key = (account, peer)
        vector = self.vectors.get(key)
        if vector is None:
            return
        records = self.journal.since(account, vector,
            limit=min(400, getattr(mesh, 'history_limit', 400)),
            byte_limit=min(_MAX_BYTES, getattr(mesh, 'history_bytes', _MAX_BYTES)))
        if not records:
            self._sent.pop(key, None)
            return
        signature = tuple((r['origin'], r['seq']) for r in records)
        previous = self._sent.get(key)
        if previous and previous[0] == signature and now-previous[1] < _INTERVAL:
            return
        mesh.send(peer, dict(type='facts', account=account, records=records))
        self._sent[key] = (signature, now)

    def _ack(self, account, peer, mesh):
        mesh.send(peer, dict(type='sync', account=account, records=[], vector=self.journal.vector(account)))

    def _records(self, message, mesh):
        records = message.get('records', [])
        if not isinstance(records, list) or len(records) > min(400, getattr(mesh, 'history_limit', 400)):
            raise ValueError('同步批次过大')
        if len(json.dumps(records, separators=(',', ':')).encode()) > _MAX_BYTES:
            raise ValueError('同步批次过大')
        return records

    def receive(self, peer, message, mesh, now):
        if not isinstance(peer, str) or not 1 <= len(peer) <= 100:
            return False
        account = None
        try:
            _identity(peer)
            removed = mesh.removed_devices() if hasattr(mesh, 'removed_devices') else set()
            if peer == self.device or peer in removed or self.device in removed:
                return False
            if not isinstance(message, dict):
                raise ValueError('共享同步消息无效')
            kind = message.get('type')
            if kind == 'peer_ready':
                self._send_catalog(peer, mesh, now)
                return True
            if kind == 'group_sync':
                self._receive_catalog(peer, message, mesh, now)
                return True
            account = _account(message.get('account'))
            self._allowed(peer, account)
            if kind == 'presence':
                declared = self.directory.get(peer, {}).get('accounts', [])
                if account not in {row['account'] for row in declared}:
                    raise ValueError('当前账号尚未由此成员添加')
                presence = self._presence_value(peer, account, message.get('presence'), now)
                self._set_current(peer, account, presence, now)
                return True
            if kind not in ('sync', 'facts'):
                raise ValueError('共享同步消息无效')
            records = self._records(message, mesh)
            vector = _vector(message.get('vector')) if kind == 'sync' else None
            presence = message.get('presence')
            if presence is not None:
                declared = self.directory.get(peer, {}).get('accounts', [])
                if account not in {row['account'] for row in declared}:
                    raise ValueError('当前账号尚未由此成员添加')
                self._presence_value(peer, account, presence, now)
            self.journal.merge(account, records, limit=min(400, getattr(mesh, 'history_limit', 400)))
            if presence:
                self._set_current(peer, account, presence, now)
            self.receipts[account, peer] = now
            self.errors.pop((account, peer), None)
            if kind == 'facts':
                self._ack(account, peer, mesh)
            else:
                old = self.vectors.get((account, peer))
                self.vectors[account, peer] = vector
                local = self.journal.vector(account)
                if records or (vector != old and any(seq > local.get(origin, 0) for origin, seq in vector.items())):
                    self._ack(account, peer, mesh)
                self._push_page(account, peer, mesh, now)
            return True
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            reason = str(error) if str(error).startswith(('共享', '来源尚未', '当前账号尚未', '设备心跳')) else rejection_reason(error)
            if account is None:
                self.catalog_errors[peer] = reason
            else:
                self.errors[account, peer] = reason
            return False

    def tick(self, mesh, now, presence=None):
        changed = self._update_own()
        current = None
        if presence is not None:
            current = _account(presence.get('account'))
            if current not in self.tracked:
                raise ValueError('当前账号尚未由此成员添加')
            self._presence_value(self.device, current, presence, now)
        changed |= self.current_accounts.get(self.device) != current
        self.current_accounts[self.device] = current
        self._presence = copy.deepcopy(presence)
        if mesh is None:
            return
        for peer in mesh.peer_states():
            if changed or now-self._last_catalog.get(peer, -_INTERVAL) >= _INTERVAL:
                self._send_catalog(peer, mesh, now)
                for account in self._labels():
                    self._push_page(account, peer, mesh, now)

    def snapshot(self, mesh, now):
        online = mesh.peer_states() if mesh else {}
        labels = self._labels()
        members = {device: dict(id=device, name=row['name'], online=device == self.device or device in online,
                    accounts=[item['account'] for item in row['accounts']],
                    current_account=self.current_accounts.get(device)) for device, row in self.directory.items()}
        for peer in online:
            members.setdefault(peer, dict(id=peer, name=peer, online=True, accounts=[], current_account=None))
        errors = dict(self.catalog_errors)
        for (account, peer), error in sorted(self.errors.items()):
            errors.setdefault(peer, error)
        receipts = dict(self.catalog_receipts)
        for (account, peer), at in self.receipts.items():
            receipts[peer] = max(receipts.get(peer, 0), at)
        local = {account: self.journal.vector(account) for account in labels}
        progress = {}
        for peer in online:
            state, send, receive = 'caught_up', 0, 0
            if not labels or peer not in self.catalog_receipts:
                state = 'waiting'
            for account, vector in local.items():
                remote = self.vectors.get((account, peer))
                if remote is None:
                    state = 'waiting'
                    continue
                send += sum(max(0, seq-remote.get(origin, 0)) for origin, seq in vector.items())
                receive += sum(max(0, seq-vector.get(origin, 0)) for origin, seq in remote.items())
            if state == 'caught_up' and (send or receive):
                state = 'syncing'
            if peer in self.catalog_receipts and now-self.catalog_receipts[peer] > 30:
                state = 'stale'
            if peer in errors:
                state = 'error'
            progress[peer] = dict(state=state, send=send, receive=receive)
        return dict(members=members, account_labels=labels, sync_receipts=receipts,
                    sync_progress=progress, sync_errors=errors, peer_versions=dict(self.peer_versions))
