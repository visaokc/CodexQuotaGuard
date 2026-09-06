"""Local, explicit enrollment. Discovery is not permission to meter an account."""
import time


def enroll(config, identity, now=None):
    account = identity.get('account', '')
    if identity.get('mode') != 'account' or len(account) != 64:
        raise ValueError('当前不是 ChatGPT 账号模式；中转 API 不可添加为订阅账号。')
    accounts = config.setdefault('tracked_accounts', {})
    if account not in accounts:
        accounts[account] = dict(label=identity['label'], added_at=time.time() if now is None else now,
                                 cap=config['quota'])
    return account
