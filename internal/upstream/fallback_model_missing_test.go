package upstream

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"workbuddy2api/internal/auth"
)

// issue #245：/v2 判渠道校验、回退 /console 之后拿到的 11102 不能升级成 model_blocked。
//
// 现场：15 个号跑几小时后全部软冷却、所有请求 503，日志里 11-128 与 11102 混在一起。
// 11102 走 BlockModelBackoff（TTL 6 小时起），于是「换接口重试」这一步的净效果变成
// 「把账号锁 6 小时」——几个号轮转过去，整池就红了。
//
// 回退路径上的 11102 只说明「这条路径没有这个模型」：我们是因为 /v2 判了渠道校验才
// 换过去的，/console 本来就可能不提供这个模型，这不构成「该账号缺这个模型」的证据。
func TestConsoleFallbackModelMissingIsRequestLevel(t *testing.T) {
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		if strings.Contains(r.URL.Path, "/console/chat/completions") {
			_, _ = w.Write([]byte(`{"code":11102,"msg":"model [deepseek-v4.1-flash] service info not found"}`))
			return
		}
		_, _ = w.Write([]byte(channelBody))
	}))
	defer srv.Close()

	c := New()
	c.ChatBaseCN = srv.URL
	acct := &auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}
	_, status, raw, err := c.ChatStream(acct,
		[]byte(`{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}]}`), "", ChatMeta{})
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", status, raw)
	}
	ue, ok := err.(*Error)
	if !ok {
		t.Fatalf("应返回分类信封，got %T (%v)", err, err)
	}
	if ue.Kind != ErrFallbackModelMissing {
		t.Fatalf("回退路径的 11102 应归请求级（ErrFallbackModelMissing），got %v", ue.Kind)
	}
	if len(paths) != 2 || !strings.Contains(paths[0], "/v2/") || !strings.Contains(paths[1], "/console/") {
		t.Fatalf("应当先在 /v2 被拒再换 /console，实际路径=%v", paths)
	}
}

// 主路径自己报 11102 仍是 (账号,模型) 负缓存：那是「该后端确实没有这个模型」的
// 确定性答复，与回退路径的语义不同。这条用例防止把降级写宽了。
func TestPrimaryPathModelMissingStaysModelBlocked(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"code":11102,"msg":"model [glm-5.2] service info not found"}`))
	}))
	defer srv.Close()

	c := New()
	c.ChatBaseCN = srv.URL
	acct := &auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}
	_, _, _, err := c.ChatStream(acct,
		[]byte(`{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}`), "", ChatMeta{})
	ue, ok := err.(*Error)
	if !ok {
		t.Fatalf("应返回分类信封，got %T (%v)", err, err)
	}
	if ue.Kind != ErrModelBlocked {
		t.Fatalf("主路径的 11102 应保持 ErrModelBlocked，got %v", ue.Kind)
	}
}
