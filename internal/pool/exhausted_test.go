package pool

import (
	"os"
	"testing"

	"workbuddy2api/internal/auth"
)

// 本地定制（2026-09-16）：额度耗尽跳过（pool.skip_exhausted）。
// 契约：权威余额<=0 的账号不参与选号/兜底/粘性分配/探活；估算归零不跳过；
// 未知余额不跳过；签到恢复后自动回到池子。

// setExhausted 把账号标记为"权威观测余额=0"：走 SetCreditsDetailed（签到的权威路径）。
func setExhausted(p *Pool, uid string) {
	p.SetCreditsDetailed(uid, 0, 0)
}

func TestExhaustedSkippedInPick(t *testing.T) {
	withNoPickGap(t)
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.Add(&auth.Auth{UID: "u2"})
	p.SetSkipExhausted(true)
	setExhausted(p, "u1") // u1 权威余额 0
	p.SetCredits("u2", 100)
	for i := 0; i < 20; i++ {
		got := p.Pick("")
		if got == nil || got.UID != "u2" {
			t.Fatalf("pick=%+v want u2（u1 已耗尽应被跳过）", got)
		}
	}
}

func TestExhaustedSkippedAllReturnsNil(t *testing.T) {
	// 全池耗尽 → 直接不可服务（按用户选择"直接停用"，不做无效调用）。
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.Add(&auth.Auth{UID: "u2"})
	p.SetSkipExhausted(true)
	setExhausted(p, "u1")
	setExhausted(p, "u2")
	if got := p.Pick(""); got != nil {
		t.Fatalf("全池耗尽应返回 nil，got %+v", got)
	}
	if p.ServableNow() {
		t.Fatal("全池耗尽时 ServableNow 应为 false（探活与选号同口径）")
	}
}

func TestExhaustedEstimateDoesNotSkip(t *testing.T) {
	// 关键回归：扣减内插把估算值压到 0，但 creditsKnown=false → 不得跳过。
	// 实测背景：9 个真实余额 2400+ 的账号被估算压到 0，按估算跳过会误杀。
	withNoPickGap(t)
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.SetSkipExhausted(true)
	// 未经签到观测（creditsKnown=false），直接写估算值 0（模拟扣减到 0）。
	p.mu.Lock()
	if e := p.byUID["u1"]; e != nil {
		e.credits = 0
		e.creditsKnown = false
	}
	p.mu.Unlock()
	if got := p.Pick(""); got == nil || got.UID != "u1" {
		t.Fatalf("估算归零不得跳过（未知≠耗尽），got %+v", got)
	}
}

func TestExhaustedUnknownNotSkipped(t *testing.T) {
	// 新加入账号（从未查过余额）→ creditsKnown=false → 不跳过。
	withNoPickGap(t)
	p := New("")
	p.Add(&auth.Auth{UID: "new"})
	p.SetSkipExhausted(true)
	if got := p.Pick(""); got == nil || got.UID != "new" {
		t.Fatalf("新账号（余额未知）应可选中，got %+v", got)
	}
}

func TestExhaustedReviveAfterCheckin(t *testing.T) {
	// 签到查到余额>0：ReenableIfCredits 恢复后重新可选中。
	withNoPickGap(t)
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.SetSkipExhausted(true)
	setExhausted(p, "u1")
	if got := p.Pick(""); got != nil {
		t.Fatalf("耗尽后应不可选，got %+v", got)
	}
	p.ReenableIfCredits("u1", 500) // 签到查到 500
	if got := p.Pick(""); got == nil || got.UID != "u1" {
		t.Fatalf("签到恢复后应可选中，got %+v", got)
	}
}

func TestExhaustedSkipDisabledByDefault(t *testing.T) {
	// 默认（未调用 SetSkipExhausted）不跳过：保持上游行为，零回归。
	withNoPickGap(t)
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.SetCredits("u1", 0) // 权威 0，但开关未开
	if got := p.Pick(""); got == nil || got.UID != "u1" {
		t.Fatalf("未开启跳过时应照常可选，got %+v", got)
	}
}

func TestExhaustedSkippedInAvailableUIDs(t *testing.T) {
	// 粘性分配可用列表同样剔除耗尽账号（新会话不再落到空号上）。
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.Add(&auth.Auth{UID: "u2"})
	p.SetSkipExhausted(true)
	setExhausted(p, "u1")
	p.SetCredits("u2", 100)
	uids := p.AvailableUIDs()
	if len(uids) != 1 || uids[0] != "u2" {
		t.Fatalf("AvailableUIDs=%v want [u2]", uids)
	}
}

func TestExhaustedSkippedByModelList(t *testing.T) {
	// 按模型可用列表（粘性按模型校验用）同样生效。
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.Add(&auth.Auth{UID: "u2"})
	p.SetSkipExhausted(true)
	setExhausted(p, "u2")
	p.SetCredits("u1", 100)
	uids := p.AvailableUIDsForModel("deepseek-v4")
	if len(uids) != 1 || uids[0] != "u1" {
		t.Fatalf("AvailableUIDsForModel=%v want [u1]", uids)
	}
}

func TestExhaustedStickyPickByUIDReturnsNil(t *testing.T) {
	// 粘性直取（PickByUIDForModel）对耗尽号返回 nil → handler 解绑重分配。
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	p.SetSkipExhausted(true)
	setExhausted(p, "u1")
	if got := p.PickByUIDForModel("u1", "deepseek-v4"); got != nil {
		t.Fatalf("耗尽号粘性直取应为 nil，got %+v", got)
	}
}

func TestExhaustedPersistenceRoundTrip(t *testing.T) {
	// credits_known 落盘/恢复往返：耗尽状态跨重启不丢（否则重启会把空号放回池子）。
	dir := t.TempDir()
	fp := dir + "/state.json"
	p := New(fp)
	p.Add(&auth.Auth{UID: "u1"})
	p.Add(&auth.Auth{UID: "u2"})
	setExhausted(p, "u1") // u1: credits=0, known=true
	p.SetCredits("u2", 300)
	p.Flush()

	p2 := New(fp)
	p2.SetSkipExhausted(true)
	if got := p2.Pick(""); got == nil || got.UID != "u2" {
		t.Fatalf("重启后耗尽号仍应被跳过，got %+v", got)
	}
}

func TestExhaustedLegacyStateMigration(t *testing.T) {
	// 旧 state.json 无 credits_known 字段：credits>0 → 视为已知；credits=0 → 未知（不跳）。
	dir := t.TempDir()
	fp := dir + "/state.json"
	legacy := `{"accounts":{"u1":{"credits":0},"u2":{"credits":500}}}`
	if err := os.WriteFile(fp, []byte(legacy), 0o600); err != nil {
		t.Fatal(err)
	}
	p := New(fp)
	p.SetSkipExhausted(true)
	// u1: credits=0 且旧文件无法确认是权威 0 → creditsKnown=false → 不跳（保守）；
	// u2: credits>0 → known=true → 不跳。两者都应可选。
	if got := p.Pick(""); got == nil {
		t.Fatal("旧文件迁移后两号都应可选（保守口径）")
	}
}

func TestExhaustedStatusExposesKnownFlag(t *testing.T) {
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	setExhausted(p, "u1")
	st, ok := p.Status("u1")
	if !ok {
		t.Fatal("status missing u1")
	}
	if !st.CreditsKnown || st.Credits != 0 {
		t.Fatalf("Status 应透出 credits_known=true、credits=0，got known=%v credits=%d", st.CreditsKnown, st.Credits)
	}
}
