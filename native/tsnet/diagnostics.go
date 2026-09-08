package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Keep only fixed event labels, never formatted upstream logs (which contain
// authorization URLs, node keys, IP addresses and account details).
type nodeDiagnostics struct {
	mu      sync.Mutex
	path    string
	started time.Time
	events  []map[string]string
}

func (d *nodeDiagnostics) logf(format string, args ...any) {
	message := strings.ToLower(format)
	for _, label := range []string{"registerresp", "registerreq", "auth url", "login", "netmap", "timeout", "no such host", "connection reset", "connection refused", "network is unreachable", "tls", "error", "failed"} {
		if strings.Contains(message, label) {
			d.mu.Lock()
			d.events = append(d.events, map[string]string{"at": time.Now().UTC().Format(time.RFC3339), "event": label})
			if len(d.events) > 64 {
				d.events = d.events[len(d.events)-64:]
			}
			d.mu.Unlock()
			break
		}
	}
}

func (d *nodeDiagnostics) save(state string, hasAuthURL bool, ipCount, healthCount int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	value := map[string]any{"pid": os.Getpid(), "started_at": d.started.UTC().Format(time.RFC3339),
		"checked_at": time.Now().UTC().Format(time.RFC3339), "state": state,
		"has_auth_url": hasAuthURL, "ip_count": ipCount, "health_count": healthCount, "events": d.events}
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return
	}
	// Diagnostics are expendable; failure must never interrupt authorization.
	os.WriteFile(filepath.Join(d.path, "node-diagnostics.json"), raw, 0600)
}
