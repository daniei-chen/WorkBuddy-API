package main

import (
	"strings"
	"testing"
	"time"
)

// cooldown.credit_exhausted（issue #78）：空 = 旧行为（0，硬冷却到次日 04:00）；
// 填了必须是正时长；非法值报错而不是静默放行。
func TestCreditExhaustedConfig(t *testing.T) {
	c := Default()
	if err := c.normalize(); err != nil {
		t.Fatalf("default normalize: %v", err)
	}
	if c.CreditExhaustedDur != 0 {
		t.Errorf("默认应是 0（保持旧行为），got %v", c.CreditExhaustedDur)
	}

	c = Default()
	c.Cooldown.CreditExhausted = "10m"
	if err := c.normalize(); err != nil {
		t.Fatalf("10m: %v", err)
	}
	if c.CreditExhaustedDur != 10*time.Minute {
		t.Errorf("10m 解析成 %v", c.CreditExhaustedDur)
	}

	for _, bad := range []string{"不是时长", "-5m", "0s"} {
		c = Default()
		c.Cooldown.CreditExhausted = bad
		err := c.normalize()
		if err == nil {
			t.Errorf("%q 应报错（正时长校验）", bad)
			continue
		}
		if !strings.Contains(err.Error(), "cooldown.credit_exhausted") {
			t.Errorf("%q 的报错没点出键名：%v", bad, err)
		}
	}
}
