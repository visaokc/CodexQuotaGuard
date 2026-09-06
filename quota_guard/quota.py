import hashlib
import base64
import json
import time
import urllib.error
import urllib.request
import tomllib
from pathlib import Path

USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'


def identity(home):
    """Read the selected auth mode, NOT Cockpit/CC Switch's saved account list."""
    home = Path(home)
    try:
        auth = json.loads((home/'auth.json').read_text(encoding='utf-8'))
        cfg = tomllib.loads((home/'config.toml').read_text(encoding='utf-8')) if (home/'config.toml').exists() else {}
        if cfg.get('profile'):
            cfg.update(cfg.get('profiles', {}).get(cfg['profile'], {}))
        provider = cfg.get('model_provider', 'openai')
        provider_cfg = cfg.get('model_providers', {}).get(provider, {})
        if (provider != 'openai' or auth.get('auth_mode') == 'apikey' or auth.get('OPENAI_API_KEY')
                or provider_cfg.get('base_url')):
            return dict(mode='api', account='', label='API / 自定义 Provider 模式', plan='', multiplier=1)
        t = auth.get('tokens') or {}
        aid = t.get('account_id')
        if not aid or not t.get('access_token'):
            return dict(mode='none', account='', label='未登录 ChatGPT 账号', plan='', multiplier=1)
        claims = {}
        try:
            part = t.get('id_token', '').split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(part + '='*((-len(part)) % 4)))
        except (ValueError, IndexError, UnicodeError):
            pass
        auth_claims = claims.get('https://api.openai.com/auth', {})
        return dict(mode='account', account=hashlib.sha256(aid.encode()).hexdigest(),
                    label=claims.get('email') or ('ChatGPT · '+aid[:8]),
                    plan=auth_claims.get('chatgpt_plan_type', ''),
                    revision=((home/'auth.json').stat().st_mtime_ns,
                              (home/'config.toml').stat().st_mtime_ns if (home/'config.toml').exists() else 0),
                    multiplier=2.5 if cfg.get('service_tier') in ('fast', 'priority') else 1)
    except (OSError, ValueError):
        return dict(mode='none', account='', label='无法读取当前登录配置', plan='', multiplier=1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a credential to an unexpected redirect target.
        raise RuntimeError('额度接口发生重定向，已停止读取')


def normalize(payload, account, now):
    limit = payload.get('rate_limit') or {}
    windows = [limit.get('primary_window'), limit.get('secondary_window')]
    weekly = next((w for w in windows if w and w.get('limit_window_seconds') == 604800), None)
    if not weekly:
        raise RuntimeError('当前账号未返回 7 天主额度池；不使用其他额度池代替')
    used = float(weekly['used_percent'])
    reset = float(weekly.get('reset_at') or now + weekly['reset_after_seconds'])
    if not 0 <= used <= 100:
        raise RuntimeError('额度数据超出有效范围')
    return dict(account=account, used=used, reset_at=reset, at=now)


def read_quota(home, now=None, expected_account=None):
    now = time.time() if now is None else now
    try:
        ident = identity(home)
        if ident['mode'] != 'account' or (expected_account is not None and ident['account'] != expected_account):
            raise RuntimeError('当前登录不属于待查询的订阅账号，已停止额度请求')
        auth = json.loads((Path(home)/'auth.json').read_text(encoding='utf-8'))
        tokens = auth.get('tokens') or {}
        token, account = tokens.get('access_token'), tokens.get('account_id')
        if not token or not account:
            raise RuntimeError('请先在 Codex 登录 ChatGPT 账号；API Key 不属于订阅周额度')
        if expected_account is not None and hashlib.sha256(account.encode()).hexdigest() != expected_account:
            raise RuntimeError('读取凭据时账号发生变化，已停止额度请求')
        req = urllib.request.Request(USAGE_URL, headers={
            'Authorization': 'Bearer '+token, 'ChatGPT-Account-Id': account,
            'Accept': 'application/json', 'User-Agent': 'CodexQuotaGuard/0.1'})
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            payload = json.load(response)
        if payload.get('account_id', account) != account:
            raise RuntimeError('额度响应与本机登录账号不一致')
        # The hub receives a one-way account identifier, never email or credentials.
        account_hash = hashlib.sha256(account.encode()).hexdigest()
        return normalize(payload, account_hash, now)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(f'额度读取被拒绝（HTTP {e.code}）；请在 Codex 刷新登录。工具不会修改登录信息') from None
        raise RuntimeError(f'额度读取失败（HTTP {e.code}）') from None
    except (OSError, ValueError, KeyError) as e:
        raise RuntimeError('额度读取失败：'+type(e).__name__) from None
