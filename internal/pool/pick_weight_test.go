package pool

import (
	"testing"
	"time"

	"workbuddy2api/internal/auth"
)

// 手写权重（issue #187）只缩放抽签概率，不改变候选资格。
//
// 语义定案：**乘数**而非固定优先级。理由写在 auth.Auth.Weight 上——固定优先会让
// 低权重号一直闲置、积分过期作废，而那正是同一条 issue 里另一半诉求要避免的。
func TestPickHonoursManualWeight(t *testing.T) {
	withNoPickGap(t)
	p := New("")
	heavy := &auth.Auth{UID: "heavy", Weight: 9}
	light := &auth.Auth{UID: "light"}
	p.Add(heavy)
	p.Add(light)

	const rounds = 4000
	counts := map[string]int{}
	for i := 0; i < rounds; i++ {
		counts[p.Pick("").UID]++
	}
	share := float64(counts["heavy"]) / rounds
	// 两个号条件完全相同（都未用过、credits 都为 0），所以抽签比例理论上
	// 就是 9:1 → 0.9。留足容差只为抗随机波动，不放宽到「比另一个多就行」——
	// 那样权重失效（乘数没生效）也可能因为随机偏差而通过。
	if share < 0.80 {
		t.Errorf("权重 9 的账号只被抽中 %.1f%%，期望接近 90%%（counts=%v）", share*100, counts)
	}
}

// 不设权重（0）与显式 1.0 必须等价：否则「默认值」会随文件里有没有这个键而漂移。
func TestManualWeightOneEqualsUnset(t *testing.T) {
	withNoPickGap(t)
	p := New("")
	p.Add(&auth.Auth{UID: "u1", Weight: 1})
	p.Add(&auth.Auth{UID: "u2"})

	const rounds = 2000
	counts := map[string]int{}
	for i := 0; i < rounds; i++ {
		counts[p.Pick("").UID]++
	}
	share := float64(counts["u1"]) / rounds
	if share < 0.45 || share > 0.55 {
		t.Errorf("权重 1 与未设置应当等价，实际占比 %.1f%%（counts=%v）", share*100, counts)
	}
}

// 权重不能越过健康门槛：冷却中的号即使权重拉满也不该被选中。
func TestManualWeightDoesNotBypassCooling(t *testing.T) {
	withNoPickGap(t)
	p := New("")
	p.Add(&auth.Auth{UID: "cooling", Weight: 100})
	p.Add(&auth.Auth{UID: "ok"})
	p.Cooldown("cooling", CoolSoft, time.Hour, "test")

	for i := 0; i < 50; i++ {
		got := p.Pick("")
		if got == nil || got.UID != "ok" {
			t.Fatalf("冷却中的高权重号被选中了：%+v", got)
		}
	}
}
