import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from athena.gpu_runtime import GPUInstaller,runtime_python,runtime_ready,ManagedGPU
from athena.embeddings import LocalEmbeddings,settings

class GPUTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.driver_patch=patch('athena.gpu_runtime.check_driver');self.driver_patch.start()
    def tearDown(self):self.driver_patch.stop();self.temp.cleanup()
    def environment(self):
        python=runtime_python(self.root);python.parent.mkdir(parents=True);python.touch()
    def test_success_only_after_actual_probe_and_isolated_pip(self):
        self.environment();installer=GPUInstaller(self.root)
        with patch('athena.gpu_runtime.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='NVIDIA RTX')),patch.object(installer,'command') as run:
            installer.run()
        self.assertTrue(runtime_ready(self.root));self.assertEqual(installer.status()['status'],'ready')
        pip,probe=[c.args[0] for c in run.call_args_list]
        self.assertEqual(pip[0],str(runtime_python(self.root)))
        self.assertIn('--cache-dir',pip);self.assertIn(str(self.root/'pip-cache'),pip);self.assertIn('--isolated',pip);self.assertIn('https://pypi.org/simple',pip)
        self.assertIn('--probe',probe)
    def test_failed_probe_does_not_mark_runtime_ready(self):
        self.environment();installer=GPUInstaller(self.root)
        with patch('athena.gpu_runtime.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='NVIDIA RTX')),patch.object(installer,'command',side_effect=[None,ValueError('failed')]):
            installer.run()
        self.assertFalse(runtime_ready(self.root));self.assertEqual(installer.status()['status'],'error')
        self.assertIn('драйвер',installer.status()['error'])
    def test_no_nvidia_does_not_download(self):
        installer=GPUInstaller(self.root)
        with patch('athena.gpu_runtime.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='Intel')),patch.object(installer,'command') as run:
            installer.run()
        run.assert_not_called();self.assertEqual(installer.status()['status'],'error')
    def test_duplicate_start_and_model_identity(self):
        installer=GPUInstaller(self.root);installer.worker=SimpleNamespace(is_alive=lambda:True);installer.job={'status':'running'}
        self.assertEqual(installer.start()['status'],'running')
        config=settings({'device':'gpu'})
        self.assertEqual(ManagedGPU(self.root,config).model_id,LocalEmbeddings('.',config).model_id)

    def test_old_driver_does_not_download(self):
        installer=GPUInstaller(self.root)
        with patch('athena.gpu_runtime.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='NVIDIA')),patch('athena.gpu_runtime.check_driver',side_effect=ValueError('Обновите драйвер')),patch.object(installer,'command') as run:
            installer.run()
        run.assert_not_called();self.assertEqual(installer.status()['status'],'error')

    def test_new_runtime_activates_provider_without_restarting_workspace(self):
        from athena.server import Workspace
        with patch('athena.server.STATE',self.root/'recent.json'):
            workspace=Workspace()
            workspace.set_semantic({'enabled':False,'settings':{'device':'gpu'}})
            before=workspace.embedder.model_id
            self.environment();(self.root/'gpu-ready.json').write_text('{}')
            workspace.refresh_gpu_provider()
            self.assertIsInstance(workspace.embedder,ManagedGPU)
            self.assertEqual(workspace.embedder.model_id,before)
            self.assertFalse(workspace.semantic_enabled)
            self.assertIsNone(workspace.semantic_worker)
            workspace.close()

    def test_real_package_progress_and_installing_phase(self):
        from athena.gpu_runtime import download_progress
        result=download_progress(['Downloading package.whl (10 MB)','Progress 0 of 1000','Progress 500 of 1000'])
        self.assertEqual(result['percent'],50)
        self.assertEqual(result['total'],1000)
        self.assertNotIn('percent',download_progress(['Progress 12 of 0']))
        self.assertEqual(download_progress(['Progress 1000 of 1000','Installing collected packages: gpu']),{'installing':True})

    def test_install_subprocess_uses_data_directory_for_temporary_files(self):
        import sys
        self.root.mkdir(exist_ok=True)
        installer=GPUInstaller(self.root)
        installer.command([sys.executable,'-c','import tempfile; print(tempfile.gettempdir())'],'Check',30)
        self.assertIn(str((self.root/'tmp'/'gpu-install').resolve()),(self.root/'gpu-install.log').read_text())
