package session

import (
 "path/filepath"
 "testing"
 "time"
 "workbuddy2api/internal/redisstore"
)

func TestRestorePreservesRemainingBindingLifetime(t *testing.T) {
 path:=filepath.Join(t.TempDir(),"binds.json")
 store,err:=redisstore.NewFileBinds(path,nil);if err!=nil{t.Fatal(err)}
 store.SetBind("existing","good",time.Minute)
 expiry:=store.LoadExpiringBinds()["existing"].Expires
 restored,err:=redisstore.NewFileBinds(path,nil);if err!=nil{t.Fatal(err)}
 router:=New(Config{TTL:24*time.Hour,Store:restored})
 router.LoadFromStoreWithExpiry()
 if got:=router.entries["existing"];got.uid!="good"||!got.lastActive.Add(router.cfg.TTL).Equal(expiry){t.Fatal("restart extended expiry",got,expiry)}
 router.gcOnce(expiry.Add(time.Nanosecond))
 if _,ok:=router.entries["existing"];ok{t.Fatal("restored binding outlived persisted expiry")}
}
