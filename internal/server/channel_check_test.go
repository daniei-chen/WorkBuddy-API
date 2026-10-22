package server

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/upstream"
)

// 渠道校验的处置：**换路径兜底 + 绝不禁用**（2026-10-17 用户报告）。
//
// 现场：上游对 /v2/chat/completions 新增渠道强校验，被校验到的号在面板上显示
// 「已禁用（需重新登录）」—— 用户以为凭证坏了去重扫，扫完还是被禁。实际是凭封套里
// 那句历史遗留的 `request illegal` 落进了「账号被封 → 硬禁用」，而账号本身是好的。
// 官方客户端走的 /console 端点并未加强校验（报告方实测：换过去就通了）。

// channelCheckBody 一次渠道校验的封套。业务码拼接构造，避免手抄整串出错。
func channelCheckBody() string {
	code := "1" + "1" + "1" + "2" + "8"
	return `{"code":` + code + `,"msg":"request illegal","requestId":"x",` +
		`"displayTips":{"zh":"这次调用没通过渠道校验，可能不是官方客户端发出的。",` +
		`"en":"This call failed channel verification, possibly because it was not sent from an official client."}}`
}

func sseResponse(status int, body string) *http.Response {
	ct := "application/json"
	if status == 200 {
		ct = "text/event-stream"
	}
	return &http.Response{
		StatusCode: status,
		Header:     http.Header{"Content-Type": []string{ct}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func TestChatFallsBackToConsoleOnChannelCheck(t *testing.T) {
	var paths []string
	up := &upstream.Client{
		HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			paths = append(paths, r.URL.Path)
			if strings.Contains(r.URL.Path, "/v2/chat/completions") {
				return sseResponse(400, channelCheckBody()), nil
			}
			return sseResponse(200, sseOK), nil
		})},
		ChatBaseCN:    "https://fake.example",
		BillingBaseCN: "https://fake.example",
	}
	p := testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at1", ExpiresAt: 9999999999})
	h := NewHandler(Config{Pool: p, Upstream: up, SoftCooldown: 600 * time.Second})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"hi"}]}`)))
	if rec.Code != 200 {
		t.Fatalf("应经 /console 兜底成功，got code=%d body=%s", rec.Code, rec.Body.String())
	}
	var sawV2, sawConsole bool
	for _, p := range paths {
		sawV2 = sawV2 || strings.Contains(p, "/v2/chat/completions")
		sawConsole = sawConsole || strings.Contains(p, "/console/chat/completions")
	}
	if !sawV2 || !sawConsole {
		t.Fatalf("应先在 /v2 被拒再换 /console，实际路径=%v", paths)
	}
	st, _ := p.Status("u1")
	if st.Disabled {
		t.Error("换路径已经成功，账号不该被禁用")
	}
	if st.Cooling {
		t.Error("换路径已经成功，账号不该被冷却")
	}
}

func TestChatChannelCheckNeverDisables(t *testing.T) {
	// 两条路径都被渠道校验拒：软冷却（退出轮换、别被反复选中），但**不禁用** ——
	// 禁用会写「需重新登录」，把用户指向错误的排查方向（凭证是好的）。
	up := &upstream.Client{
		HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return sseResponse(400, channelCheckBody()), nil
		})},
		ChatBaseCN:    "https://fake.example",
		BillingBaseCN: "https://fake.example",
	}
	p := testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at1", ExpiresAt: 9999999999})
	h := NewHandler(Config{Pool: p, Upstream: up, SoftCooldown: 600 * time.Second})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"hi"}]}`)))
	st, _ := p.Status("u1")
	if st.Disabled {
		t.Fatalf("渠道校验**绝不能**禁用账号（用户报告的症状）：reason=%q", st.DisabledReason)
	}
	if !st.Cooling || st.CoolKind != "soft_rate" {
		t.Fatalf("两条路径都被拒时应软冷却让它退出轮换：%+v", st)
	}
	if !strings.Contains(st.Reason, "channel check") {
		t.Errorf("冷却原因应写明是渠道校验，便于排障：%q", st.Reason)
	}
}

// issue #245：回退到 /console 拿到的 11102 不得写 (账号,模型) 负缓存。
//
// 现场：15 个号跑几小时后全部软冷却、所有请求 503。原因是回退路径的 11102 被当成
// 「该账号缺这个模型」，写了 6 小时模型级冷却，逐个账号轮过去就把整池打穿。
func TestConsoleFallbackModelMissingDoesNotBlockModel(t *testing.T) {
	up := &upstream.Client{
		HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if strings.Contains(r.URL.Path, "/console/chat/completions") {
				return sseResponse(400, `{"code":11102,"msg":"model [glm-5.2] service info not found"}`), nil
			}
			return sseResponse(400, channelCheckBody()), nil
		})},
		ChatBaseCN:    "https://fake.example",
		BillingBaseCN: "https://fake.example",
	}
	p := testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at1", ExpiresAt: 9999999999})
	h := NewHandler(Config{Pool: p, Upstream: up, SoftCooldown: 600 * time.Second})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"hi"}]}`)))
	if rec.Code == 200 {
		t.Fatalf("上游两条路径都失败，不该返回 200：%s", rec.Body.String())
	}

	st, _ := p.Status("u1")
	if len(st.RateLimitedModels) != 0 {
		t.Errorf("回退路径的 11102 不该写模型冷却，got %+v", st.RateLimitedModels)
	}
	if st.ConsecutiveFails != 0 {
		t.Errorf("失败的是路径不是账号，不该喂连败计数，got %d", st.ConsecutiveFails)
	}
	if st.Cooling || st.Disabled {
		t.Errorf("账号不该被冷却/禁用: cooling=%v disabled=%v", st.Cooling, st.Disabled)
	}
}
