package server
import("net/http/httptest";"testing")
func TestTenantSessionNamespaceStableAndIsolated(t *testing.T){
 r:=httptest.NewRequest("POST","/v1/chat/completions",nil)
 r.Header.Set("X-WB-Tenant","key:1/upstream:0")
 a:=tenantSessionKey(r,"internal-key","cn","conversation")
 if a!=tenantSessionKey(r,"internal-key","cn","conversation"){t.Fatal("unstable")}
 if a==tenantSessionKey(r,"internal-key","global","conversation"){t.Fatal("realm collision")}
 r.Header.Set("X-WB-Tenant","key:2/upstream:0")
 if a==tenantSessionKey(r,"internal-key","cn","conversation"){t.Fatal("tenant collision")}
 if tenantSessionKey(r,"internal-key","cn","")!=""{t.Fatal("invented session")}
}
func TestTenantSessionNamespacePreservesDirectClientKeys(t *testing.T){
 r:=httptest.NewRequest("POST","/v1/chat/completions",nil)
 if got:=tenantSessionKey(r,"internal-key","cn","conv-1");got!="conv-1"{t.Fatalf("legacy key changed: %q",got)}
 r.Header.Set("X-WB-Tenant",string(make([]byte,257)))
 if got:=tenantSessionKey(r,"internal-key","cn","conv-1");got!="conv-1"{t.Fatalf("invalid tenant changed legacy key: %q",got)}
}
