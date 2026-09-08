// Private userspace tailnet node. No system service, routes, or public listener.
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"sync"
	"time"

	"tailscale.com/tailcfg"
	"tailscale.com/tsnet"
)

const maxMessage = 240 * 1024
const syncPort = "24817"

type initConfig struct{ Dir, Hostname, Token string }
type packet struct {
	IP       string          `json:"ip"`
	Envelope json.RawMessage `json:"envelope"`
}
type bridge struct {
	s           *tsnet.Server
	client      *http.Client
	inbox       chan packet
	token       string
	mu          sync.Mutex
	ready       bool
	diagnostics *nodeDiagnostics
}

func writeJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(value)
}
func (b *bridge) known(ctx context.Context, ip netip.Addr) bool {
	lc, err := b.s.LocalClient()
	if err != nil {
		return false
	}
	st, err := lc.Status(ctx)
	if err != nil {
		return false
	}
	for _, p := range st.Peer {
		for _, a := range p.TailscaleIPs {
			if a == ip {
				return true
			}
		}
	}
	return false
}

func (b *bridge) incoming(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" || r.URL.Path != "/message" {
		http.NotFound(w, r)
		return
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		http.Error(w, "peer", 400)
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxMessage))
	if err != nil || !json.Valid(raw) {
		http.Error(w, "message", 400)
		return
	}
	select {
	case b.inbox <- packet{host, raw}:
		writeJSON(w, map[string]bool{"ok": true})
	default:
		http.Error(w, "busy", 503)
	}
}

func (b *bridge) control(w http.ResponseWriter, r *http.Request) {
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+b.token)) != 1 {
		http.Error(w, "unauthorized", 401)
		return
	}
	if r.Method != "POST" {
		http.Error(w, "method", 405)
		return
	}
	// urllib sends Connection: close. Drain the bounded request body before
	// replying so Windows does not reset a socket with unread incoming bytes.
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxMessage+1024))
	if err != nil {
		http.Error(w, "request too large", 413)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	lc, err := b.s.LocalClient()
	if err != nil {
		http.Error(w, "node unavailable", 503)
		return
	}
	switch r.URL.Path {
	case "/status":
		st, err := lc.Status(ctx)
		if err != nil {
			http.Error(w, "status unavailable", 503)
			return
		}
		b.mu.Lock()
		ready := b.ready
		b.mu.Unlock()
		// Only expose the fields the application needs, never node keys or user profiles.
		if b.diagnostics != nil {
			b.diagnostics.save(st.BackendState, st.AuthURL != "", len(st.TailscaleIPs), len(st.Health))
		}
		tailnet := ""
		if st.CurrentTailnet != nil {
			tailnet = st.CurrentTailnet.Name
		}
		writeJSON(w, map[string]any{"state": st.BackendState, "ips": st.TailscaleIPs, "auth_url": st.AuthURL, "ready": ready,
			"tailnet": tailnet, "peer_count": len(st.Peer)})
	case "/login":
		if err := lc.StartLoginInteractive(ctx); err != nil {
			http.Error(w, "login unavailable", 503)
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
	case "/logout":
		if err := lc.Logout(ctx); err != nil {
			http.Error(w, "logout unavailable", 503)
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
	case "/receive":
		out := []packet{}
		for len(out) < 32 {
			select {
			case p := <-b.inbox:
				out = append(out, p)
			default:
				writeJSON(w, out)
				return
			}
		}
		writeJSON(w, out)
	case "/send", "/route":
		var p packet
		if err := json.NewDecoder(bytes.NewReader(raw)).Decode(&p); err != nil {
			http.Error(w, "request", 400)
			return
		}
		ip, err := netip.ParseAddr(p.IP)
		if err != nil || !b.known(ctx, ip) {
			http.Error(w, "not a tailnet peer", 400)
			return
		}
		if r.URL.Path == "/route" {
			result, err := lc.Ping(ctx, ip, tailcfg.PingDisco)
			if err != nil {
				writeJSON(w, map[string]string{"Err": "route probe failed"})
				return
			}
			writeJSON(w, result)
			return
		}
		if len(p.Envelope) > maxMessage || !json.Valid(p.Envelope) {
			http.Error(w, "message", 400)
			return
		}
		req, err := http.NewRequestWithContext(ctx, "POST", "http://"+net.JoinHostPort(ip.String(), syncPort)+"/message", bytes.NewReader(p.Envelope))
		if err != nil {
			http.Error(w, "request", 400)
			return
		}
		resp, err := b.client.Do(req)
		if err != nil {
			http.Error(w, "peer unreachable", 502)
			return
		}
		defer resp.Body.Close()
		io.Copy(io.Discard, io.LimitReader(resp.Body, 1024))
		if resp.StatusCode != 200 {
			http.Error(w, "peer rejected message", 502)
			return
		}
		writeJSON(w, map[string]bool{"ok": true})
	default:
		http.NotFound(w, r)
	}
}

func main() {
	var cfg initConfig
	// Configuration and IPC capability arrive over an inherited pipe, not argv.
	dec := json.NewDecoder(os.Stdin)
	if dec.Decode(&cfg) != nil || len(cfg.Token) < 32 || cfg.Dir == "" {
		os.Exit(2)
	}
	diagnostics := &nodeDiagnostics{path: cfg.Dir, started: time.Now()}
	s := &tsnet.Server{Dir: cfg.Dir, Hostname: cfg.Hostname, UserLogf: diagnostics.logf, Logf: diagnostics.logf}
	if err := s.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "embedded node startup failed")
		os.Exit(1)
	}
	defer s.Close()
	b := &bridge{s: s, token: cfg.Token, inbox: make(chan packet, 64), diagnostics: diagnostics}
	b.client = &http.Client{Transport: &http.Transport{DialContext: s.Dial, MaxIdleConnsPerHost: 2}, Timeout: 5 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		os.Exit(1)
	}
	api := &http.Server{Handler: http.HandlerFunc(b.control), ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 8 * time.Second, WriteTimeout: 8 * time.Second, MaxHeaderBytes: 8192}
	go api.Serve(ln)
	json.NewEncoder(os.Stdout).Encode(map[string]string{"api": "http://" + ln.Addr().String()})
	go func() {
		tl, err := s.Listen("tcp", ":"+syncPort)
		if err != nil {
			return
		}
		b.mu.Lock()
		b.ready = true
		b.mu.Unlock()
		server := &http.Server{Handler: http.HandlerFunc(b.incoming), ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 8 * time.Second, WriteTimeout: 8 * time.Second, MaxHeaderBytes: 8192}
		server.Serve(tl)
	}()
	// Parent closure (including crashes) ends the helper; Windows also uses a Job.
	io.Copy(io.Discard, os.Stdin)
	api.Close()
}
