package upstream

import (
	"net/http"
	"net/url"
	"testing"
)

// TestTransportHonorsEnvironmentProxy 出站必须走标准环境变量代理（issue #136）。
//
// 自定义 Transport 不设 Proxy 字段时它是 nil —— 环境变量被**静默忽略**，
// 「容器里 curl 经代理能通、网关却仍直连」这种故障没有任何日志可查。
// 这里钉两件事：Proxy 必须非 nil（正是缺陷所在），且回环地址按 stdlib 约定绕过。
//
// 为什么不直接设 HTTP_PROXY 断言返回值：`http.ProxyFromEnvironment` 会把环境
// 解析结果**缓存在进程内**（sync.Once），测试进程里谁先调用谁定终身，按环境
// 断言会随测试顺序漂移。真实行为由 ProxyFromEnvironment 保证，我们只需保证
// 「确实接上了它」。
func TestTransportHonorsEnvironmentProxy(t *testing.T) {
	tr := newTransport()
	if tr.Proxy == nil {
		t.Fatal("Transport.Proxy 为 nil：HTTP_PROXY / HTTPS_PROXY / NO_PROXY 会被静默忽略")
	}

	loop, err := url.Parse("http://127.0.0.1:7863/healthz")
	if err != nil {
		t.Fatal(err)
	}
	req := &http.Request{URL: loop}
	got, err := tr.Proxy(req)
	if err != nil {
		t.Fatalf("Proxy(loopback) 报错：%v", err)
	}
	if got != nil {
		t.Fatalf("回环地址不该走代理，实际得到 %v", got)
	}
}
