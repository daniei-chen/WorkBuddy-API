package server

// Observation only: account selection, priority, retries and prompts remain in
// their original implementation. No credential or message content enters this log.
import (
    "encoding/json"
    "log"
    "net/http"
    "regexp"
)
var auditHash = regexp.MustCompile(`^[a-f0-9]{64}$`)
func auditAttempt(r *http.Request, attempt int, uid, model, realm, phase string, status int) {
    bound := func(value string) string { if len(value)>128 { return value[:128] }; return value }
    configHash := r.Header.Get("X-WB-Config-Hash")
    if !auditHash.MatchString(configHash) { configHash="" }
    raw,_ := json.Marshal(map[string]any{"event":"workbuddy_attempt","request_id":bound(r.Header.Get("X-Request-ID")),
        "attempt":attempt,"account_uid":bound(uid),"resolved_model":bound(model),"realm":realm,
        "phase":phase,"status":status,"config_hash":configHash})
    log.Printf("%s",raw)
}
