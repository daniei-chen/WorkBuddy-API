package auth

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// 账号手写权重（issue #187）：面板写在 auths/*.json 顶层 `weight`，上游必须
// 解析出来，并且在刷新 token 整份重写文件时保留（否则等于静默清零）。
func TestParseWeight(t *testing.T) {
	nested := []byte(`{"auth":{"accessToken":"at","refreshToken":"rt","expiresAt":1,"domain":"d","realm":"cn"},
		"account":{"uid":"u1","nickname":"n"},"weight":2.5}`)
	a, err := Parse(nested)
	if err != nil {
		t.Fatalf("nested parse: %v", err)
	}
	if a.Weight != 2.5 {
		t.Errorf("嵌套形的顶层 weight 没解析出来：%v", a.Weight)
	}

	flat := []byte(`{"accessToken":"at","expiresAt":1,"uid":"u2","weight":3}`)
	b, err := Parse(flat)
	if err != nil {
		t.Fatalf("flat parse: %v", err)
	}
	if b.Weight != 3 {
		t.Errorf("扁平形的 weight 没解析出来：%v", b.Weight)
	}

	none := []byte(`{"accessToken":"at","expiresAt":1,"uid":"u3"}`)
	c, err := Parse(none)
	if err != nil {
		t.Fatalf("no-weight parse: %v", err)
	}
	if c.Weight != 0 {
		t.Errorf("没写 weight 时应当是「未设置」(0)：%v", c.Weight)
	}
}

// 负数/非有限值当「未设置」；越界的合法值保留原样（钳制在 PickWeight 做，
// 面板要按原值回显「用户填了什么」）。
func TestParseWeightSanitizesOnlyInvalid(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want float64
	}{
		{"负数当未设置", `{"accessToken":"at","expiresAt":1,"uid":"u","weight":-2}`, 0},
		{"太小保留原样", `{"accessToken":"at","expiresAt":1,"uid":"u","weight":0.01}`, 0.01},
		{"太大保留原样", `{"accessToken":"at","expiresAt":1,"uid":"u","weight":1e9}`, 1e9},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, err := Parse([]byte(tc.raw))
			if err != nil {
				t.Fatalf("parse: %v", err)
			}
			if a.Weight != tc.want {
				t.Errorf("weight=%v want %v", a.Weight, tc.want)
			}
		})
	}
}

func TestPickWeightClamps(t *testing.T) {
	cases := []struct {
		name string
		in   float64
		want float64
	}{
		{"未设置=1", 0, 1},
		{"正常值原样", 2.5, 2.5},
		{"低于下限钳到 MinWeight", 0.01, MinWeight},
		{"高于上限钳到 MaxWeight", 1e9, MaxWeight},
		{"负数=默认", -3, 1},
		{"NaN=默认", math.NaN(), 1},
		{"Inf=默认", math.Inf(1), 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a := &Auth{Weight: tc.in}
			if got := a.PickWeight(); got != tc.want {
				t.Errorf("PickWeight(%v)=%v want %v", tc.in, got, tc.want)
			}
		})
	}
}

func TestSaveAtomicPreservesWeight(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "workbuddy-u1.json")
	if err := os.WriteFile(path, []byte(`{"accessToken":"old","expiresAt":1,"uid":"u1","weight":4}`), 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := Parse(mustRead(t, path))
	if err != nil {
		t.Fatal(err)
	}
	a.FilePath = path
	a.AccessToken = "new" // 模拟刷新后的写回
	if err := a.SaveAtomic(); err != nil {
		t.Fatalf("save: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(mustRead(t, path), &doc); err != nil {
		t.Fatal(err)
	}
	if got, ok := doc["weight"].(float64); !ok || got != 4 {
		t.Errorf("刷新写回后 weight 丢了：%v", doc["weight"])
	}
}

// 没有权重的文件不该被凭空加上 weight 键（否则每次刷新都在所有账号上留噪音）。
func TestSaveAtomicOmitsUnsetWeight(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "workbuddy-u2.json")
	if err := os.WriteFile(path, []byte(`{"accessToken":"old","expiresAt":1,"uid":"u2"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := Parse(mustRead(t, path))
	if err != nil {
		t.Fatal(err)
	}
	a.FilePath = path
	if err := a.SaveAtomic(); err != nil {
		t.Fatalf("save: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(mustRead(t, path), &doc); err != nil {
		t.Fatal(err)
	}
	if _, ok := doc["weight"]; ok {
		t.Errorf("未设置权重时不该写入 weight 键：%v", doc["weight"])
	}
}
