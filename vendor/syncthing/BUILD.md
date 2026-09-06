# Embedded Syncthing v2.1.3-cqg1

This is a modified build of Syncthing v2.1.3, licensed under MPL-2.0.
Upstream source: https://github.com/syncthing/syncthing/tree/v2.1.3
Complete upstream source archive: https://github.com/syncthing/syncthing/archive/refs/tags/v2.1.3.zip

The complete modified MPL-covered file is distributed in
`modified-source/lib/dialer/public.go`. Copy it over the same upstream path.
The only modification makes `SetTCPOptions(dialerConn)` retain proxy socket
defaults instead of rejecting opaque HTTP CONNECT / SOCKS connection wrappers.
TLS encryption, peer certificate identity, and relay authentication are unchanged.

Build on Windows amd64 with Go 1.27.1:

```
go run build.go -version v2.1.3-cqg1 build syncthing
```

Copy the resulting `syncthing.exe` here and update `BINARY_SHA256` in
`quota_guard/autolink.py` if rebuilding. Compiler/source paths and build timestamp
may change binary hashes. The distributed executable SHA256 is:
`380963659e52201f14accf46bd400487e88d7a1e8e21f209e3c841f0464e0da5`.

The app starts a private instance only for enrolled subscription accounts.
Only its dedicated AES-GCM ciphertext mailbox folder is shared, never Codex
credentials, prompts, session files, or the local ledger database.
Global discovery and public relays belong to the Syncthing ecosystem; this
project does not operate them. Their availability is not guaranteed.
