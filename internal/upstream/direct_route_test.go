package upstream

import (
	"net/http"
	"testing"

	"workbuddy2api/internal/auth"
)

// 账号级强制直连（issue #223）：proxy=__direct__ 必须绕过进程级环境代理，
// 且与普通线路分开连接池。
func TestDirectRouteBypassesEnvProxy(t *testing.T) {
	ConfigureProxies(map[string]string{
		DirectRouteName: "http://should-be-ignored:8080",
		"route-a":       "http://a.example:8080",
	})
	t.Cleanup(func() { ConfigureProxies(nil) })

	tr, err := TransportFor(DirectRouteName)
	if err != nil {
		t.Fatalf("直连线路不应报错：%v", err)
	}
	if tr.Proxy != nil {
		t.Fatal("直连 Transport 的 Proxy 必须为 nil，否则会继续走环境代理")
	}
	again, _ := TransportFor(DirectRouteName)
	if again != tr {
		t.Error("直连 Transport 没有复用同一实例（连接池会分裂）")
	}
	route, _ := TransportFor("route-a")
	if route == tr {
		t.Error("直连与普通线路共用了 Transport —— 连接池会串")
	}

	c := New()
	client, err := c.clientFor(&auth.Auth{UID: "u-direct", ProxyName: DirectRouteName}, false)
	if err != nil {
		t.Fatalf("clientFor 直连失败：%v", err)
	}
	got, ok := client.Transport.(*http.Transport)
	if !ok || got.Proxy != nil {
		t.Fatalf("账号直连没有落到无代理 Transport：%#v", client.Transport)
	}
}
