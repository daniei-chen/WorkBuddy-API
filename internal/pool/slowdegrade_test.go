// 慢速降权（本地定制）测试：触发、滞回、半开探测恢复、相对门槛防雪崩、
// 粘性逃逸与兜底可选。阈值全部用 SetSlowDegrade 注入小值（100ms 级），不依赖
// 真实时长——判慢逻辑只看注入的 TTFB 样本值。
package pool

import (
	"testing"
	"time"

	"workbuddy2api/internal/auth"
)

// newSlowTestPool 构造小阈值测试池：绝对下限 100ms、连续 3 笔触发、降权 50ms。
// fresh 池的观测环为空（< slowRingMinForRelative），判定走绝对线，样本值可预测。
func newSlowTestPool(t *testing.T) *Pool {
	t.Helper()
	p := New("")
	p.SetSlowDegrade(true, 100*time.Millisecond, 3, 50*time.Millisecond)
	p.Add(&auth.Auth{UID: "u1"})
	return p
}

// TestNoteSpeedTriggersSlowDegradeAtStreak 连续 3 笔慢样本达阈触发降权：
// 前两笔只累计 streak（账号保持可选），第 3 笔触发 slowUntil 并出池，
// /status 呈现 slow_degrade 文案；兜底 lane 仍可选（半开探测语义）。
func TestNoteSpeedTriggersSlowDegradeAtStreak(t *testing.T) {
	p := newSlowTestPool(t)
	for n := 1; n < 3; n++ {
		p.NoteSpeed("u1", 200*time.Millisecond)
		streak, until, _ := p.slowStateOf("u1")
		if streak != n {
			t.Fatalf("第 %d 笔后 slowStreak=%d want %d", n, streak, n)
		}
		if !until.IsZero() {
			t.Fatalf("第 %d 笔不应触发降权（未达阈值 3）", n)
		}
		if uids := p.AvailableUIDs(); len(uids) != 1 {
			t.Fatalf("未达阈时账号应保持可选（第 %d 笔）", n)
		}
	}
	p.NoteSpeed("u1", 200*time.Millisecond) // 第 3 笔：达阈
	streak, until, _ := p.slowStateOf("u1")
	if streak != 0 {
		t.Fatalf("达阈后 streak 应清零供下一轮累计，got %d", streak)
	}
	if until.IsZero() || !time.Now().Before(until) {
		t.Fatalf("第 3 笔应触发慢速降权，slowUntil=%v", until)
	}
	if uids := p.AvailableUIDs(); len(uids) != 0 {
		t.Fatalf("降权期账号不应出现在可用列表, got %v", uids)
	}
	// 兜底 lane 仍可选（降权号参与兜底 = 半开探测，与连败降权同口径）。
	if got := p.Pick(""); got == nil || got.UID != "u1" {
		t.Fatalf("降权号应参与全冷却兜底，got %v", got)
	}
	st, _ := p.Status("u1")
	if !st.Cooling || st.Reason != slowDegradeReason || st.CoolKind != "slow_degrade" {
		t.Fatalf("降权期 Status 应呈非健康+慢速文案: cooling=%v reason=%q kind=%q",
			st.Cooling, st.Reason, st.CoolKind)
	}
	if st.SlowUntil.IsZero() || st.SlowStreak != 0 {
		t.Fatalf("Status 应透出 slow_until 且 streak 已清零: streak=%d until=%v",
			st.SlowStreak, st.SlowUntil)
	}
}

// TestNoteSpeedHealthySampleResetsStreak 健康样本打断连续口径：两笔慢 + 一笔
// 健康 + 两笔慢（共 4 笔慢）不触发——单笔尖峰不清账，偶发慢不累计成降权。
func TestNoteSpeedHealthySampleResetsStreak(t *testing.T) {
	p := newSlowTestPool(t)
	p.NoteSpeed("u1", 200*time.Millisecond)
	p.NoteSpeed("u1", 200*time.Millisecond)
	p.NoteSpeed("u1", 50*time.Millisecond) // 健康样本：streak 归零
	streak, until, _ := p.slowStateOf("u1")
	if streak != 0 || !until.IsZero() {
		t.Fatalf("健康样本应清零 streak 且不触发降权: streak=%d until=%v", streak, until)
	}
	p.NoteSpeed("u1", 200*time.Millisecond)
	p.NoteSpeed("u1", 200*time.Millisecond)
	_, until, _ = p.slowStateOf("u1")
	if !until.IsZero() {
		t.Fatalf("重计后仅 2 笔慢不应触发降权，until=%v", until)
	}
	p.NoteSpeed("u1", 200*time.Millisecond) // 第 3 笔连续慢
	_, until, _ = p.slowStateOf("u1")
	if until.IsZero() {
		t.Fatal("重计满 3 笔连续慢应触发降权")
	}
}

// TestSlowDegradeHealthyProbeClears 半开探测恢复：降权期内兜底探测拿到健康
// 样本 → slowUntil 立即清空回池（与 NoteSuccess 清 degradeUntil 同口径）。
func TestSlowDegradeHealthyProbeClears(t *testing.T) {
	p := newSlowTestPool(t)
	for i := 0; i < 3; i++ {
		p.NoteSpeed("u1", 200*time.Millisecond)
	}
	_, until, _ := p.slowStateOf("u1")
	if until.IsZero() {
		t.Fatal("前置：应已触发降权")
	}
	p.NoteSpeed("u1", 50*time.Millisecond) // 探测成功
	_, until, _ = p.slowStateOf("u1")
	if !until.IsZero() {
		t.Fatalf("健康探测样本应清 slowUntil，got %v", until)
	}
	if uids := p.AvailableUIDs(); len(uids) != 1 {
		t.Fatalf("恢复后账号应回到可选列表, got %v", uids)
	}
}

// TestSlowDegradeExpiresAndRestreaks 降权到期自然回池；回池后再满连续慢样本
// 会再次降权（不延长语义下，到期后的新一轮从零累计）。
func TestSlowDegradeExpiresAndRestreaks(t *testing.T) {
	p := newSlowTestPool(t)
	for i := 0; i < 3; i++ {
		p.NoteSpeed("u1", 200*time.Millisecond)
	}
	_, first, _ := p.slowStateOf("u1")
	time.Sleep(60 * time.Millisecond) // 等 50ms 降权期过期
	if uids := p.AvailableUIDs(); len(uids) != 1 {
		t.Fatalf("降权到期后账号应回池, got %v", uids)
	}
	for i := 0; i < 3; i++ {
		p.NoteSpeed("u1", 200*time.Millisecond)
	}
	_, second, _ := p.slowStateOf("u1")
	if !second.After(first) {
		t.Fatalf("到期后重新达阈应设新的降权截止: first=%v second=%v", first, second)
	}
}

// TestSlowDegradeNoExtendWhileDegraded 降权期内再次达阈不延长（与 NoteFailures
// 同哲学：防尖峰把降权越堆越厚）。达阈清零后需重新累计 3 笔才会进「不延长」分支。
func TestSlowDegradeNoExtendWhileDegraded(t *testing.T) {
	p := newSlowTestPool(t)
	for i := 0; i < 3; i++ {
		p.NoteSpeed("u1", 200*time.Millisecond)
	}
	_, until1, _ := p.slowStateOf("u1")
	for i := 0; i < 3; i++ { // 降权期内（兜底 lane 的慢样本）重新满 3 笔
		p.NoteSpeed("u1", 200*time.Millisecond)
	}
	_, until2, _ := p.slowStateOf("u1")
	if !until2.Equal(until1) {
		t.Fatalf("降权期内再达阈不应延长: until1=%v until2=%v", until1, until2)
	}
}

// TestSlowDegradeRelativeGuardBlocksUniformCongestion 相对门槛防雪崩（核心）：
// 全池均匀慢（环中位数 200ms）时，250ms 样本越绝对线（100ms）但未越 3×中位数
// （600ms）→ 不判慢、不降级——上游整体拥堵时谁都不该被单独摘除。
func TestSlowDegradeRelativeGuardBlocksUniformCongestion(t *testing.T) {
	p := newSlowTestPool(t)
	p.Add(&auth.Auth{UID: "u2"})
	// 填满观测环（64 笔 200ms）：全部经 u2 喂入。
	for i := 0; i < slowRingSize; i++ {
		p.NoteSpeed("u2", 200*time.Millisecond)
	}
	for i := 0; i < 3; i++ {
		p.NoteSpeed("u1", 250*time.Millisecond)
	}
	streak, until, _ := p.slowStateOf("u1")
	if streak != 0 || !until.IsZero() {
		t.Fatalf("全池均匀慢不应触发降权: streak=%d until=%v", streak, until)
	}
}

// TestSlowDegradeRelativeTriggersIndividualSlowLane 相对门槛的正向面：环中位数
// 100ms（其他号都健康）时，500ms 样本同时越绝对线与 3×中位数 → 判慢并照常触发。
func TestSlowDegradeRelativeTriggersIndividualSlowLane(t *testing.T) {
	p := newSlowTestPool(t)
	p.Add(&auth.Auth{UID: "u2"})
	for i := 0; i < slowRingSize; i++ {
		p.NoteSpeed("u2", 100*time.Millisecond)
	}
	for i := 0; i < 3; i++ {
		p.NoteSpeed("u1", 500*time.Millisecond)
	}
	_, until, _ := p.slowStateOf("u1")
	if until.IsZero() {
		t.Fatal("显著慢于池内中位数的号应触发降权")
	}
}

// TestPickByUIDSkipsSlowDegraded 粘性逃逸：slowUntil 生效时 PickByUIDForModel
// 返回 nil（handler 据此解绑并回落轮换），健康样本恢复后恢复直取。
func TestPickByUIDSkipsSlowDegraded(t *testing.T) {
	p := newSlowTestPool(t)
	if got := p.PickByUIDForModel("u1", ""); got == nil {
		t.Fatal("前置：健康账号粘性直取应命中")
	}
	for i := 0; i < 3; i++ {
		p.NoteSpeed("u1", 200*time.Millisecond)
	}
	if got := p.PickByUIDForModel("u1", ""); got != nil {
		t.Fatalf("降权期粘性直取应失败（nil），got %v", got.UID)
	}
	p.NoteSpeed("u1", 50*time.Millisecond) // 探测成功即恢复
	if got := p.PickByUIDForModel("u1", ""); got == nil {
		t.Fatal("恢复后粘性直取应重新命中")
	}
}

// TestSetSlowDegradeDisabled 显式关停：NoteSpeed 只进观测环，不累计 streak、
// 不降权（逃生门，回到引入前行为）。
func TestSetSlowDegradeDisabled(t *testing.T) {
	p := newSlowTestPool(t)
	p.SetSlowDegrade(false, 0, 0, 0) // 只翻开关；非正值保留原参数
	for i := 0; i < 5; i++ {
		p.NoteSpeed("u1", 200*time.Millisecond)
	}
	streak, until, _ := p.slowStateOf("u1")
	if streak != 0 || !until.IsZero() {
		t.Fatalf("关停后不应累计/降权: streak=%d until=%v", streak, until)
	}
	if uids := p.AvailableUIDs(); len(uids) != 1 {
		t.Fatalf("关停后账号应保持可选, got %v", uids)
	}
}

// TestNoteSpeedIgnoresZeroAndUnknown 防御边界：无观测（ttfb=0）与未知 uid 均
// 空操作（auths 目录热加载/轮转竞态下 handler 仍可能喂到已删除账号）。
func TestNoteSpeedIgnoresZeroAndUnknown(t *testing.T) {
	p := newSlowTestPool(t)
	p.NoteSpeed("u1", 0)
	p.NoteSpeed("ghost", 200*time.Millisecond)
	if streak, until, _ := p.slowStateOf("u1"); streak != 0 || !until.IsZero() {
		t.Fatalf("零观测不应累计: streak=%d until=%v", streak, until)
	}
}
