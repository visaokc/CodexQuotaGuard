# Controller actions

`WebController(folder, config, database, demo=False, startup_enabled=True)` has only three public methods: `snapshot()`, `command(action, payload={})`, and `window_action(action)`. Host owns `_start()`, `_close()`, `_set_hidden(bool)`, and sets `_window_handler(action)`. All fields and internal methods are private to prevent automatic bridge exposure.

Snapshot returns `{version, view, settings, accounts, pairing, update, notices}`. `view` retains the Engine field shapes for identity, summary, peers, analytics, recovery and sync progress, with nested allowlists. No credentials, auth URLs, raw transports, journal records or external exception bodies appear. `view.sync_caption` and `view.sync_confirmed_at` represent receipt-confirmed ledger progress; the timestamp is null unless every online peer is caught up and has a receipt. The backend does not reinterpret accounting data. The frontend filters retired devices from selectors and chart aggregates.

`accounts` is an array of `{account,label,added_at,cap}`. `settings` retains allowlisted saved settings and includes `multiplier` and account-scoped `device_order`. `pairing` has a `stage` and safe connection fields `state,ready,tailnet,ips,connected`. `notices` contains internally generated Engine messages; the frontend should deduplicate messages rather than repeat every poll. `update` is `{status,ready,version?}`.

Commands return `{ok:true,data:...}` or `{ok:false,error:string}`. Mutations are serialized; a concurrent command returns a busy error, while snapshot remains available throughout network calls. The frontend awaits commands and shows progress without rebuilding the page or closing an open selector.

| Action | Payload | Result data |
|---|---|---|
| refresh | empty | null; wakes existing Engine |
| account_scan, account_add | empty | `{mode,account,label,plan,multiplier}` |
| account_remove | `account,confirmed:true` | null; preserves existing ledger files |
| account_history | `account` | `{summary,history}` from existing Ledger |
| cap_save | `account,cap` | null; existing Engine.set_cap validates current identity |
| limit_toggle | `enabled`, and for enabling `account,confirmed:true` | null; checks administrator and actual EXE paths |
| restore | empty | null; existing Engine restore command |
| note_save | `account,device,text` | null; at most 40 characters; empty clears note |
| color_save | `account,device,color` | null; one of eight palette colors |
| device_order_save | `account,devices:[id,...]` | null; complete active-device order, saved per account locally without restarting Engine |
| device_remove | `account,device,confirmed:true` | null; existing group removal command, no history deletion |
| settings_save | `settings:{...}`, plus `confirmed:true` when enabling automatic limit or changing its protected targets | null |
| programs_discover | empty | array of EXE paths |
| pair_generate | empty | `{code}` if ready, otherwise `{pending:true}` |
| pair_join | `code,confirmed:true` | null; current CQG4 only |
| tailscale_login | empty | `{opened,message?}`; validates URL and opens browser itself |
| tailscale_switch | `confirmed:true` | `{opened:true}`; keeps history and group |
| connection_save | optional `rendezvous_url,relay_token,stun_url,force_relay` | null; omitted keys preserve saved values, explicit empty strings clear them; credentials are write-only |
| update_check | optional `manual` default true | safe update status; downloads and verifies signature |
| update_install | `confirmed:true` | `{installing:true}`; requires verified staged offer and no active restriction |
| diagnostics | empty | existing numeric-only sync report |

`settings_save` accepts only `name,theme,quota_display,autostart,auto_update,auto_block,codex_home,interval,program_paths,quota,multiplier`. Quota display is `account` or `personal`; no accounting formula is changed. Runtime-changing settings restart the existing Engine. Display-only settings do not.

Preset colors: `#669cff`, `#f6b763`, `#55d6be`, `#cd8af0`, `#ed8299`, `#c6db76`, `#f39777`, `#62cde2`.

For pairing, `pair_generate` starts the embedded Tailscale node if needed. On pending, offer `tailscale_login`; after `pairing.ready` becomes true, retry generation in the explicit pairing interaction. The code is returned only by the requested action and never retained in periodic snapshots.

Window actions: `minimize,close,quit,drag,resize,maximize,shown,browse_program,export_diagnostics`. Close hides to tray. Host confirms restoring active protection before quit, then calls `_close()`, which stops the Engine and restores its paused processes. Update installation invokes host `quit` only after the verified existing updater launches successfully. Automatic updates follow the saved user preference and retry deferred installation every 30 seconds while limit protection is active.

Theme is system (default), light or dark. Saving theme is display-only and never restarts the Engine.

- `maintenance_toggle`: `{enabled: bool}`; available to any bound member in shared billing, targets only the caller. Queue preserves command timestamp and writes an authenticated profile fact. The Settings switch reflects `shared_group.maintenance_enabled`.
