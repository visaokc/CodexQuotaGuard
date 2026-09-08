package main

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDiagnosticsNeverPersistUpstreamSecrets(t *testing.T) {
	d := &nodeDiagnostics{path: t.TempDir(), started: time.Now()}
	for i := 0; i < 100; i++ {
		d.logf("login error: %s", "https://login.tailscale.com/a/SECRET nodekey:PRIVATE user@example.com")
	}
	d.save("NeedsLogin", true, 0, 1)
	raw, err := os.ReadFile(filepath.Join(d.path, "node-diagnostics.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"SECRET", "PRIVATE", "example.com", "https://"} {
		if strings.Contains(string(raw), secret) {
			t.Fatal("diagnostic leaked secret")
		}
	}
	if len(d.events) != 64 {
		t.Fatal("unbounded diagnostic history")
	}
}

func TestIPCRejectsMissingCapability(t *testing.T) {
	b := &bridge{token: strings.Repeat("s", 32)}
	w := httptest.NewRecorder()
	b.control(w, httptest.NewRequest("POST", "/status", nil))
	if w.Code != 401 {
		t.Fatal(w.Code)
	}
}

func TestIncomingLimitsAndBackpressure(t *testing.T) {
	b := &bridge{inbox: make(chan packet, 1)}
	for i, want := range []int{200, 503} {
		r := httptest.NewRequest("POST", "/message", strings.NewReader(`{"ciphertext":"opaque"}`))
		r.RemoteAddr = "100.64.0.1:1234"
		w := httptest.NewRecorder()
		b.incoming(w, r)
		if w.Code != want {
			t.Fatalf("%d: %d", i, w.Code)
		}
	}
	r := httptest.NewRequest("POST", "/message", strings.NewReader(strings.Repeat("x", maxMessage+1)))
	r.RemoteAddr = "100.64.0.1:1234"
	w := httptest.NewRecorder()
	b.incoming(w, r)
	if w.Code != 400 {
		t.Fatal(w.Code)
	}
}
