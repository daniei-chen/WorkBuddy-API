package server

import (
 "crypto/sha256"
 "encoding/hex"
 "net/http"
 "strings"
)

// Called after API authentication. Manager replaces this header with a namespace
// based on the resolved key and upstream; user supplied headers are never forwarded.
func tenantSessionKey(r *http.Request, apiKey, realm, key string) string {
 if key==""{return ""}
 tenant:=strings.TrimSpace(r.Header.Get("X-WB-Tenant"))
 if len(tenant)>256{tenant=""}
 // Existing direct gateway clients retain their original sticky and request-ID
 // keys. Manager supplies a trusted tenant header for cross-key isolation.
 if tenant==""{return key}
 identity:=sha256.Sum256([]byte(apiKey+"\x00"+tenant+"\x00"+realm))
 sum:=sha256.Sum256([]byte(hex.EncodeToString(identity[:])+"\x00"+key))
 return "ns2:"+hex.EncodeToString(sum[:])
}
