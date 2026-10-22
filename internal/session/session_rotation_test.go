package session

import (
	"testing"
	"time"
)

// 本地定制（2026-09-16）：新会话分配由「FNV 哈希取模」改为「顺序轮转」。
// 覆盖契约：依次取下一个账号、到尾回绕、账号被移出后顺延不跳漏、同会话保持粘性。
func TestRotationSequentialThenWrap(t *testing.T) {
	avail := []string{"a1", "a2", "a3"}
	r := New(Config{
		TTL:       time.Minute,
		Available: func() []string { return avail },
	})
	want := []string{"a1", "a2", "a3", "a1"} // 第 4 个新会话回绕到开头
	for i, w := range want {
		got, ok := r.ResolveForModel("k"+string(rune('1'+i)), "")
		if !ok || got != w {
			t.Fatalf("第 %d 个新会话 = %q（ok=%v），want %q", i+1, got, ok, w)
		}
	}
}

func TestRotationSkipsRemovedAccount(t *testing.T) {
	avail := []string{"a1", "a2", "a3"}
	r := New(Config{
		TTL:       time.Minute,
		Available: func() []string { return avail },
	})
	if got, _ := r.ResolveForModel("k1", ""); got != "a1" {
		t.Fatalf("首个会话 = %q want a1", got)
	}
	// a1 冷却/额度耗尽被移出可用列表：下一个新会话应顺延到 a1 之后（a2），
	// 既不重放 a1、也不跳漏 a2。
	avail = []string{"a2", "a3"}
	if got, _ := r.ResolveForModel("k2", ""); got != "a2" {
		t.Fatalf("移出 a1 后新会话 = %q want a2（顺延不跳漏）", got)
	}
}

func TestRotationSameKeySticky(t *testing.T) {
	r := New(Config{
		TTL:       time.Minute,
		Available: func() []string { return []string{"a1", "a2"} },
	})
	u1, _ := r.ResolveForModel("k1", "")
	u2, _ := r.ResolveForModel("k1", "")
	if u1 != u2 {
		t.Fatalf("同会话两次解析应同账号：%q vs %q", u1, u2)
	}
	// 新会话轮转到另一个账号（而非哈希碰巧相同的旧行为）。
	u3, _ := r.ResolveForModel("k2", "")
	if u3 == u1 {
		t.Fatalf("新会话应轮转到下一个账号，实际仍为 %q", u3)
	}
}

func TestRotationCursorIndependentOfKey(t *testing.T) {
	// 轮转游标只由"上一个被分配的账号"决定，与 key 内容无关：
	// 交换 key 顺序不应改变分配序列（区别于原哈希实现）。
	avail := []string{"a1", "a2", "a3"}
	r1 := New(Config{TTL: time.Minute, Available: func() []string { return append([]string{}, avail...) }})
	r2 := New(Config{TTL: time.Minute, Available: func() []string { return append([]string{}, avail...) }})
	seq1 := []string{}
	for _, k := range []string{"alpha", "beta", "gamma"} {
		u, _ := r1.ResolveForModel(k, "")
		seq1 = append(seq1, u)
	}
	seq2 := []string{}
	for _, k := range []string{"zzz", "yyy", "xxx"} {
		u, _ := r2.ResolveForModel(k, "")
		seq2 = append(seq2, u)
	}
	for i := range seq1 {
		if seq1[i] != seq2[i] {
			t.Fatalf("轮转序列应与 key 无关：%v vs %v", seq1, seq2)
		}
	}
}
