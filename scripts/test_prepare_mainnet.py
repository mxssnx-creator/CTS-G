"""Preparing shared code must not activate either exchange connection."""
import json
import pathlib
import runpy
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
prepare = runpy.run_path(str(ROOT/'deploy/prepare-mainnet-release.py'))['prepare']


class PrepareMainnetTests(unittest.TestCase):
    def test_stopped_mainnet_gets_shared_profile_without_start_or_peer_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td); revision = 'a'*40
            data = root/'var/lib/cts-gx';data.mkdir(parents=True)
            stop = data/'STOP-bingx-x01';stop.write_text('operator stopped\n')
            stamp = stop.stat().st_mtime_ns
            overlay = data/'overlay-bingx-x01.json'
            overlay.write_text(json.dumps(dict(minPf=1.26, marginMode='isolated')))
            profile = root/'opt/cts-gx-releases'/revision[:12]/'server/pulse/connection_profile.py'
            profile.parent.mkdir(parents=True)
            profile.write_text((ROOT/'server/pulse/connection_profile.py').read_text())
            calls = []
            def command(*args):
                calls.append(args)
                if args[:2] == ('systemctl','show'):
                    return '0' if 'x01' in args[2] else '789'
                if args[0] == 'git':return revision
                self.assertEqual(args,('systemctl','daemon-reload'))
                return ''
            result = prepare(revision,root,command)
            self.assertFalse(result['mainnetStarted'])
            self.assertTrue(result['stopPreserved'])
            self.assertTrue(result['vstProcessUnchanged'])
            self.assertEqual(stop.stat().st_mtime_ns,stamp)
            current = json.loads(overlay.read_text())
            self.assertEqual(current['marginMode'],'isolated')
            self.assertEqual(current['minPf'],1.05)
            self.assertEqual(current['symbolCap'],20)
            self.assertEqual(current['baseEvalPosCount'],30)
            self.assertTrue(all(not any(x in c for x in ('start','restart','enable')) for c in calls))

    def test_running_or_unstopped_mainnet_is_not_modified(self):
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td)
            for pid in ('0','123'):
                calls=[]
                def command(*args):calls.append(args);return pid
                with self.assertRaises(RuntimeError):prepare('b'*40,root,command)
                self.assertEqual(len(calls),1)
                self.assertEqual(list(root.iterdir()),[])


if __name__ == '__main__':unittest.main()
