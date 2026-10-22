package auth

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// 账号级出口线路名（issue #210）：面板写在 auths/*.json 顶层 `proxy`，
// 上游必须解析出来（并刷新 token 时保留，否则等于静默解绑）。
func TestParseProxyName(t *testing.T) {
	nested := []byte(`{"auth":{"accessToken":"at","refreshToken":"rt","expiresAt":1,"domain":"d","realm":"cn"},
		"account":{"uid":"u1","nickname":"n"},"proxy":"route-a"}`)
	a, err := Parse(nested)
	if err != nil {
		t.Fatalf("nested parse: %v", err)
	}
	if a.ProxyName != "route-a" {
		t.Errorf("嵌套形的顶层 proxy 没解析出来：%q", a.ProxyName)
	}

	flat := []byte(`{"accessToken":"at","expiresAt":1,"uid":"u2","proxy":"  route-b  "}`)
	b, err := Parse(flat)
	if err != nil {
		t.Fatalf("flat parse: %v", err)
	}
	if b.ProxyName != "route-b" {
		t.Errorf("扁平形的 proxy 没解析（且应去空白）：%q", b.ProxyName)
	}

	none := []byte(`{"accessToken":"at","expiresAt":1,"uid":"u3"}`)
	c, err := Parse(none)
	if err != nil {
		t.Fatalf("no-proxy parse: %v", err)
	}
	if c.ProxyName != "" {
		t.Errorf("没写 proxy 时不该编造：%q", c.ProxyName)
	}
}

func TestSaveAtomicPreservesProxyName(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "workbuddy-u1.json")
	if err := os.WriteFile(path, []byte(`{"accessToken":"old","expiresAt":1,"uid":"u1","proxy":"route-a"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := Parse(mustRead(t, path))
	if err != nil {
		t.Fatal(err)
	}
	a.FilePath = path
	a.AccessToken = "new-token" // 模拟刷新后的写回
	if err := a.SaveAtomic(); err != nil {
		t.Fatalf("save: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(mustRead(t, path), &doc); err != nil {
		t.Fatal(err)
	}
	if doc["proxy"] != "route-a" {
		t.Errorf("刷新 token 后线路绑定丢了（doc=%v）—— 出口会静默改变", doc)
	}

	// 未绑线路的账号：写回时不该凭空多出 proxy 键
	path2 := filepath.Join(dir, "workbuddy-u2.json")
	if err := os.WriteFile(path2, []byte(`{"accessToken":"old","expiresAt":1,"uid":"u2"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	b, _ := Parse(mustRead(t, path2))
	b.FilePath = path2
	b.AccessToken = "new"
	if err := b.SaveAtomic(); err != nil {
		t.Fatal(err)
	}
	var doc2 map[string]any
	_ = json.Unmarshal(mustRead(t, path2), &doc2)
	if _, ok := doc2["proxy"]; ok {
		t.Errorf("没绑线路的账号被写入了空 proxy 键：%v", doc2)
	}
}

func mustRead(t *testing.T, p string) []byte {
	t.Helper()
	raw, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

var _ = strings.TrimSpace
