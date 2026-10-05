import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from server import offsitebackup

@unittest.skipUnless(shutil.which('git') and shutil.which('openssl'),'Git and OpenSSL required')
class EncryptedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.private=self.root/'private.pem';self.public=self.root/'public.pem'
        self.run_command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=fixture',
                          '-keyout',str(self.private),'-out',str(self.public)])
        self.snapshot=self.root/'snapshot.tar.gz';self.snapshot.write_bytes(b'private fixture only\x00'*100)
    def run_command(self,command):
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        return result.stdout
    def decrypt(self,cipher,output):
        return subprocess.run(['openssl','cms','-decrypt','-binary','-inform','DER','-in',str(cipher),
              '-inkey',str(self.private),'-recip',str(self.public),'-out',str(output)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
    def test_authenticated_roundtrip_and_tamper_rejection(self):
        cipher=self.root/'cipher.cms';plain=self.root/'plain.tar.gz'
        offsitebackup.encrypt(self.snapshot,self.public,cipher)
        self.assertNotIn(b'private fixture',cipher.read_bytes())
        self.assertEqual(self.decrypt(cipher,plain).returncode,0)
        self.assertEqual(plain.read_bytes(),self.snapshot.read_bytes())
        damaged=bytearray(cipher.read_bytes());damaged[-1]^=1;cipher.write_bytes(damaged)
        self.assertNotEqual(self.decrypt(cipher,plain).returncode,0)
    def test_fast_forward_remote_ciphertext_only(self):
        remote=self.root/'remote.git';repo=self.root/'backup.git'
        self.run_command(['git','init','--bare',str(remote)])
        first=offsitebackup.publish(self.snapshot,self.public,repo,str(remote))
        self.snapshot.write_bytes(b'second private fixture')
        second=offsitebackup.publish(self.snapshot,self.public,repo,str(remote))
        git=['git','--git-dir',str(remote)]
        self.assertEqual(self.run_command(git+['rev-parse',second['commit']+'^']).decode().strip(),first['commit'])
        names=self.run_command(git+['ls-tree','--name-only',second['commit']]).decode().splitlines()
        self.assertEqual(sorted(names),['README.md','snapshot.cms','snapshot.json'])
        metadata=json.loads(self.run_command(git+['show',second['commit']+':snapshot.json']))
        self.assertEqual(metadata['format'],'workbuddy-recovery-cms-v1')
        cipher=self.root/'fetched.cms';cipher.write_bytes(self.run_command(git+['show',second['commit']+':snapshot.cms']))
        plain=self.root/'fetched.tar.gz'
        self.assertEqual(self.decrypt(cipher,plain).returncode,0)
        self.assertEqual(plain.read_bytes(),self.snapshot.read_bytes())
        self.assertEqual(self.run_command(git+['for-each-ref','--format=%(refname)']).decode().splitlines(),['refs/heads/'+offsitebackup.BRANCH])
