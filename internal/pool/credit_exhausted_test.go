package pool

import (
	"testing"
	"time"

	"workbuddy2api/internal/auth"
)

// 默认（没有 cooldown.credit_exhausted）必须保持旧行为：硬冷却到次日 04:00。
// 这条是「不配就一点都别变」的护栏 —— 老部署升级后行为必须逐字不变。
func TestCreditExhaustedDefaultKeepsHardCooldown(t *testing.T) {
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.CooldownUntilTomorrow4AM("u1", "余额不足")

	st, ok := p.Status("u1")
	if !ok {
		t.Fatal("no status")
	}
	if !st.Cooling {
		t.Fatalf("should be cooling: %+v", st)
	}
	if st.Until.Hour() != 4 {
		t.Errorf("until hour=%d want 4（旧行为是硬冷却到次日 04:00）", st.Until.Hour())
	}
	if e := p.byUID["u1"]; e.coolKind != CoolHard {
		t.Errorf("coolKind=%v want CoolHard", e.coolKind)
	}
}

// 配了 credit_exhausted：改成软冷却、时长就是配置值，不再挂到次日 04:00。
// 这是 issue #78 的核心诉求：用完积分的号只退出这一小段。
func TestCreditExhaustedShortCooldown(t *testing.T) {
	p := New("")
	p.SetCreditExhausted(10 * time.Minute)
	p.Add(&auth.Auth{UID: "u1"})

	before := time.Now()
	p.CooldownUntilTomorrow4AM("u1", "余额不足")
	after := time.Now()

	st, ok := p.Status("u1")
	if !ok {
		t.Fatal("no status")
	}
	if !st.Cooling {
		t.Fatalf("should be cooling: %+v", st)
	}
	if e := p.byUID["u1"]; e.coolKind != CoolSoft {
		t.Errorf("coolKind=%v want CoolSoft", e.coolKind)
	}
	d := st.Until.Sub(before)
	if d < 9*time.Minute || d > 11*time.Minute {
		t.Errorf("冷却跨度 %v 不是配置的 10m（±1m 容差，now 落在 %v..%v）", d, before, after)
	}
}

// 窗口过去就回到候选池（不是等一整天）。
func TestCreditExhaustedReturnsAfterWindow(t *testing.T) {
	p := New("")
	p.SetCreditExhausted(5 * time.Minute)
	p.Add(&auth.Auth{UID: "u1"})
	p.CooldownUntilTomorrow4AM("u1", "余额不足")

	if st, _ := p.Status("u1"); !st.Cooling {
		t.Fatal("冷却期内应处于 cooling")
	}
	// 把截止时间拨到过去 = 窗口过去
	p.mu.Lock()
	p.byUID["u1"].until = time.Now().Add(-time.Second)
	p.mu.Unlock()
	st, _ := p.Status("u1")
	if st.Cooling {
		t.Errorf("窗口过后不该还在冷却：%+v", st)
	}
}
