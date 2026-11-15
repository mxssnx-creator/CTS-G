"""Actual sidecar recovery cycle and installer credential preflight with fake IO only."""
import os,subprocess,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'server/pulse'),str(Path(__file__).resolve().parent)]
import pulse_http as h
import test_startup_controls as startup

class RecoveryErrors(startup.StartupControls):
    def test_error_replies_never_restart_services(self):
        for error in ['NOAUTH Authentication required.','DENIED protected mode','ERR rejected','WRONGTYPE wrong kind','NOPERM denied']:
            with self.subTest(error=error):
                self.credentials={'api_key':error,'api_secret':error};self.calls.clear()
                self.recover_once()
                self.assertEqual(self.mutations(),[])

class SettingsErrors(unittest.TestCase):
    def test_bad_status_and_error_text_never_become_settings(self):
        for code,text in [(1,'setMinPf\n0\n'),(0,'NOAUTH denied\n0\n'),(0,'DENIED protected mode\n0\n')]:
            with tempfile.TemporaryDirectory() as d,patch.object(h,'DIR',d):
                with self.subTest(code=code,text=text),patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=code,stdout=text)):
                    self.assertEqual(h.load_cts('bingx-x02'),{})
                    self.assertEqual(list(Path(d).iterdir()),[])
    def test_good_typed_settings_keep_their_values(self):
        with tempfile.TemporaryDirectory() as d,patch.object(h,'DIR',d),patch.object(h.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='zero\n0\nflag\nfalse\narray\n[1,2]\n')) as run:
            self.assertEqual(h.load_cts('bingx-x02'),{'zero':0,'flag':False,'array':[1,2]})
            self.assertEqual(run.call_args.kwargs['timeout'],5)

class InstallerErrors(unittest.TestCase):
    def test_real_installer_predicate_rejects_failed_replies_without_secret_output(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            cli=Path(d)/'redis-cli'
            cli.write_text('#!'+sys.executable+'\nimport os,sys\nprint(os.environ["FAKE_REDIS_REPLY"])\nsys.exit(int(os.environ["FAKE_REDIS_RC"]))\n');cli.chmod(0o700)
            for code,reply,want in [(0,'fixture-private-value',0),(0,'NOAUTH denied',1),(0,'DENIED protected mode',1),(0,'(nil)',1),(1,'fixture-private-value',1)]:
                env={'PATH':d+os.pathsep+os.environ['PATH'],'FAKE_REDIS_REPLY':reply,'FAKE_REDIS_RC':str(code),'CTS_G_ROOT':str(root)}
                result=subprocess.run(['bash','-c','source "$1/deploy/linux-common.sh"; redis_has_keys bingx-x02','test',str(root)],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,want,(reply,result.stderr));self.assertEqual(result.stdout,'');self.assertEqual(result.stderr,'')

if __name__=='__main__':unittest.main()
