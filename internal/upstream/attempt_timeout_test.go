// 尝试看门狗（AttemptTimeout，本地定制）测试：晾死的尝试在预算内被切断走轮换、
// 健康长流不被误杀（响应头到达即解除）、外层 ctx 取消不受干扰。
package upstream

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
)

// TestChatStreamAttemptTimeoutCutsSilentAttempt 核心验收点：上游收下连接但
// 不回任何响应头（拥堵期晾连接形态），AttemptTimeout 到点切断本次尝试——
// 不再吃满外层网关总预算，handler 得以轮转换号。
func TestChatStreamAttemptTimeoutCutsSilentAttempt(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		<-release // 挂住：不写响应头不写体（上游晾连接形态）
	}))
	defer srv.Close()
	defer close(release) // LIFO：先放行 handler 再关 server，避免 Close 等挂死的 handler

	c := New()
	c.ChatBaseCN = srv.URL
	c.AttemptTimeout = 150 * time.Millisecond

	start := time.Now()
	_, _, _, err := c.ChatStream(&auth.Auth{AccessToken: "at", UID: "u1"}, []byte(`{}`), "", ChatMeta{})
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("被晾死的尝试应在看门狗到点后返回错误")
	}
	if elapsed > 3*time.Second {
		t.Fatalf("尝试应在 ~150ms 被切断，实际 %v（看门狗未生效?）", elapsed)
	}
}

// TestChatStreamAttemptTimeoutSparesHealthyStream 回归核心约束：响应头按时
// 到达后看门狗必须解除——流式阶段即便远超 AttemptTimeout 也继续可读（仍由
// IdleTimeout/外层 ctx 管）。若看门狗误杀，第二帧读取会在时限处报 context canceled。
func TestChatStreamAttemptTimeoutSparesHealthyStream(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		flusher := w.(http.Flusher)
		io.Copy(io.Discard, r.Body)
		io.WriteString(w, "data: frame-1\n\n")
		flusher.Flush()
		time.Sleep(400 * time.Millisecond) // 远超下方 80ms 看门狗
		io.WriteString(w, "data: frame-2\n\n")
		flusher.Flush()
	}))
	defer srv.Close()

	c := New()
	c.ChatBaseCN = srv.URL
	c.AttemptTimeout = 80 * time.Millisecond
	c.IdleTimeout = 5 * time.Second

	rc, status, _, err := c.ChatStream(&auth.Auth{AccessToken: "at", UID: "u1"}, []byte(`{}`), "", ChatMeta{})
	if err != nil || status != 200 {
		t.Fatalf("chat: status=%d err=%v", status, err)
	}
	defer rc.Close()

	buf := make([]byte, 15) // "data: frame-N\n\n" 恰 15 字节
	if _, err := io.ReadFull(rc, buf); err != nil {
		t.Fatalf("read frame-1: %v", err)
	}
	if !strings.Contains(string(buf), "frame-1") {
		t.Fatalf("frame-1 内容不符: %q", string(buf))
	}
	// 第二帧在 400ms（>80ms 看门狗时限）才到：读得到 = 看门狗已解除。
	if _, err := io.ReadFull(rc, buf); err != nil {
		t.Fatalf("read frame-2（应跨过看门狗时限仍可读）: %v", err)
	}
	if !strings.Contains(string(buf), "frame-2") {
		t.Fatalf("frame-2 内容不符: %q", string(buf))
	}
}

// TestChatStreamParentCancelBeatsWatchdog 外层取消语义不受看门狗干扰：父 ctx
// 取消（网关/客户端先撤）时尝试立即中断，不等看门狗到点。
func TestChatStreamParentCancelBeatsWatchdog(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		<-release
	}))
	defer srv.Close()
	defer close(release)

	c := New()
	c.ChatBaseCN = srv.URL
	c.AttemptTimeout = 30 * time.Second // 看门狗远未到点

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	start := time.Now()
	_, _, _, err := c.ChatStreamContext(ctx, &auth.Auth{AccessToken: "at", UID: "u1"}, []byte(`{}`), "", ChatMeta{})
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("父 ctx 取消后尝试应立即返回错误")
	}
	if elapsed > 5*time.Second {
		t.Fatalf("父 ctx 取消应立即中断（~50ms），实际 %v", elapsed)
	}
}
