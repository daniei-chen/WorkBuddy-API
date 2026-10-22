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

// 11151「对话里有空消息」→ **400 透传**，不是 503（issue #203）。
//
// 以前它落通用 4xx：只换号不罚号，轮完一圈拿 503 收场 —— 客户端看到「服务暂时不可用」，
// 而真实原因只是对话里有一条空消息（同 body 换任何账号结果都一样）。
var emptyMsgBody = `{"code":11151,"msg":"a message has empty content, please check the conversation history and retry","requestId":"r1"}`

func TestEmptyMessageReturns400WithoutRotating(t *testing.T) {
	calls := 0
	up := &upstream.Client{
		HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			calls++
			return &http.Response{
				StatusCode: 400,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(emptyMsgBody)),
			}, nil
		})},
		ChatBaseCN:    "https://fake.example",
		BillingBaseCN: "https://fake.example",
	}
	p := testPoolWith(
		&auth.Auth{UID: "u1", AccessToken: "at1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "u2", AccessToken: "at2", ExpiresAt: 9999999999},
	)
	h := NewHandler(Config{Pool: p, Upstream: up, SoftCooldown: 600 * time.Second})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":""}]}`)))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("应回 400（请求内容问题），got %d body=%s", rec.Code, rec.Body.String())
	}
	if calls != 1 {
		t.Errorf("不该轮转（同一 body 换号结果不变，白扔配额）：上游被调用了 %d 次", calls)
	}
	if !strings.Contains(rec.Body.String(), "11151") {
		t.Errorf("应透传上游原文（含 code，客户端按它自查）：%s", rec.Body.String())
	}
	// 不罚号：两个号都不该被冷却/禁用
	for _, uid := range []string{"u1", "u2"} {
		st, _ := p.Status(uid)
		if st.Cooling || st.Disabled {
			t.Errorf("%s 不该被罚（这是请求的问题）：%+v", uid, st)
		}
	}
}

func TestClassifyEmptyMessage(t *testing.T) {
	for _, body := range []string{
		emptyMsgBody,
		`{"code":"11151","msg":"empty"}`,        // 字符串码
		`{"code":11151,"msg":"x"}`,              // 紧凑含空格差
		`{"msg":"a message has empty content"}`, // 只有文案
		`{"code":0,"msg":"存在空白消息，请补全后重试"}`,      // 中文文案
	} {
		if got := upstream.Classify(400, body); got != upstream.ErrEmptyMessage {
			t.Errorf("Classify(%q)=%v want ErrEmptyMessage", body, got)
		}
	}
	// 内容审核的 11-128 不该被这条规则吞掉（那是另一类，要降级重试）
	if got := upstream.Classify(400, `{"code":11-128,"msg":"blocked by security policy"}`); got == upstream.ErrEmptyMessage {
		t.Error("内容审核被误判成空消息")
	}
}
