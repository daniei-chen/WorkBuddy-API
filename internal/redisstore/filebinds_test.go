package redisstore
import("os";"path/filepath";"testing";"time")
func TestFileBindsRestartAndTTL(t *testing.T){
 path:=filepath.Join(t.TempDir(),"binds.json")
 f,e:=NewFileBinds(path,nil);if e!=nil{t.Fatal(e)}
 f.SetBind("ns2:fixture","a1",time.Hour)
 f.SetBind("expired","a2",-time.Second)
 restored,e:=NewFileBinds(path,nil);if e!=nil{t.Fatal(e)}
 binds:=restored.LoadBinds();if binds["ns2:fixture"]!="a1"||binds["expired"]!=""{t.Fatal(binds)}
 info,e:=os.Stat(path);if e!=nil||info.Mode().Perm()!=0600{t.Fatal(info,e)}
 restored.DelBind("ns2:fixture")
 last,e:=NewFileBinds(path,nil);if e!=nil||len(last.LoadBinds())!=0{t.Fatal(e)}
}
func TestFileBindsRejectsCorruption(t *testing.T){
 path:=filepath.Join(t.TempDir(),"binds.json");os.WriteFile(path,[]byte("{bad"),0600)
 if _,e:=NewFileBinds(path,nil);e==nil{t.Fatal("corruption accepted")}
}
