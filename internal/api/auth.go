package api

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Optional portal authentication. When PORTAL_PASSWORD is set, every route
// except /api/health, /api/auth, /api/login and the internal API (which uses
// the manager token) requires a signed session cookie. When it is empty the
// portal is open, which suits a self-hosted tool on a trusted network.

const sessionCookie = "doppio_session"
const sessionTTL = 7 * 24 * time.Hour

func (s *Server) authMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if s.portalPassword == "" {
			next.ServeHTTP(w, r)
			return
		}
		p := r.URL.Path
		if p == "/api/health" || p == "/api/auth" || p == "/api/login" || p == "/assets/doppio-logo.png" || strings.HasPrefix(p, "/api/internal/") {
			next.ServeHTTP(w, r)
			return
		}
		if p == "/login" {
			s.serveLoginPage(w)
			return
		}
		if s.validSession(r) {
			next.ServeHTTP(w, r)
			return
		}
		if strings.HasPrefix(p, "/api/") {
			writeErr(w, http.StatusUnauthorized, "authentication required")
			return
		}
		http.Redirect(w, r, "/login", http.StatusFound)
	})
}

func sessionMAC(password string, expiry int64) string {
	mac := hmac.New(sha256.New, []byte("doppio:"+password))
	fmt.Fprintf(mac, "%d", expiry)
	return hex.EncodeToString(mac.Sum(nil))
}

func (s *Server) validSession(r *http.Request) bool {
	c, err := r.Cookie(sessionCookie)
	if err != nil {
		return false
	}
	parts := strings.Split(c.Value, ".")
	if len(parts) != 2 {
		return false
	}
	expiry, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || time.Now().Unix() > expiry {
		return false
	}
	return hmac.Equal([]byte(parts[1]), []byte(sessionMAC(s.portalPassword, expiry)))
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Password string `json:"password"`
	}
	if !decode(w, r, &in) {
		return
	}
	if s.portalPassword == "" {
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
		return
	}
	if !hmac.Equal([]byte(in.Password), []byte(s.portalPassword)) {
		time.Sleep(time.Second) // slow down password guessing
		writeErr(w, http.StatusUnauthorized, "wrong password")
		return
	}
	expiry := time.Now().Add(sessionTTL).Unix()
	val := fmt.Sprintf("%d.%s", expiry, sessionMAC(s.portalPassword, expiry))
	http.SetCookie(w, &http.Cookie{
		Name: sessionCookie, Value: val, Path: "/",
		Expires: time.Unix(expiry, 0), HttpOnly: true, SameSite: http.SameSiteLaxMode,
		Secure: r.TLS != nil || r.Header.Get("X-Forwarded-Proto") == "https",
	})
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) handleLogout(w http.ResponseWriter, _ *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: sessionCookie, Value: "", Path: "/", MaxAge: -1})
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// serveLoginPage serves a dependency-free login page (the SPA itself is
// behind auth, so it cannot host the login form).
func (s *Server) serveLoginPage(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(`<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Doppio — login</title>
<style>
body{background:#0f1115;color:#e6e8ee;font-family:ui-monospace,Menlo,Consolas,monospace;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#171a21;border:1px solid #2a2f3a;border-radius:10px;padding:2rem;width:320px}
h1{font-size:1.1rem;margin:0 0 1rem}
input{width:100%;box-sizing:border-box;padding:.6rem;background:#0f1115;color:#e6e8ee;border:1px solid #2a2f3a;border-radius:6px;margin-bottom:.8rem}
button{width:100%;padding:.6rem;background:#4c8dff;border:0;border-radius:6px;color:#fff;font:inherit;cursor:pointer}
.err{color:#ff7b72;font-size:.8rem;min-height:1em;margin-bottom:.6rem}
</style></head><body>
<form id="f"><img src="/assets/doppio-logo.png" alt="Doppio" width="72" style="display:block;margin:0 auto 1rem"><h1>Doppio</h1><div class="err" id="e"></div>
<input type="password" id="p" placeholder="password" autocomplete="current-password" autofocus>
<button type="submit">Log in</button></form>
<script>
document.getElementById('f').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: document.getElementById('p').value})});
  if (r.ok) location.href = '/'; else document.getElementById('e').textContent = 'wrong password';
});
</script></body></html>`))
}
