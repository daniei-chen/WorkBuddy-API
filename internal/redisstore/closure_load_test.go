package redisstore
import("fmt";"path/filepath";"testing";"time";"os")
func TestFileBindsMaximumMirrorLoad(t *testing.T) {
 path:=filepath.Join(t.TempDir(),"binds.json")
 f,e:=NewFileBinds(path,nil);if e!=nil{t.Fatal(e)}
 expires:=time.Now().Add(time.Hour).UnixNano()
 for i:=0;i<20000;i++{f.binds[fmt.Sprintf("ns2:tenant:%05d",i)]=fileBind{UID:fmt.Sprintf("fixture-%05d",i),Expires:expires}}
 start:=time.Now()
 for i:=0;i<100;i++{f.SetBind(fmt.Sprintf("ns2:tenant:%05d",i),"fixture-unchanged",time.Hour)}
 duration:=time.Since(start)
 if len(f.LoadBinds())!=20000{t.Fatal("mirror lost entries")}
 restored,e:=NewFileBinds(path,nil);if e!=nil{t.Fatal(e)}
 if len(restored.LoadExpiringBinds())!=20000{t.Fatal("maximum mirror failed recovery")}
 info,e:=os.Stat(path);if e!=nil || info.Size()>16*1024*1024 || info.Mode().Perm()!=0600 {t.Fatal("unsafe or oversized mirror",e)}
 t.Logf("maximum mirror: 20k entries, 100 atomic fsync writes: %s, average=%s, bytes=%d",duration,duration/100,info.Size())
 // A very broad tripwire avoids environment-specific latency claims. The exact
 // measured latency is retained as evidence and must be judged against a workload.
 if duration>30*time.Second{t.Fatal("mirror write workload exceeded 30 seconds")}
}
