"""Release regression checks use a fake Docker executable, never a host daemon."""
import hashlib
import gzip
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("testpilot_release_builder", ROOT / "scripts/package_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

FAKE_DOCKER = r'''#!PYTHON
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['TESTPILOT_FAKE_LOG']).open('a') as handle:
    handle.write(json.dumps(args) + '\n')
if args[:2] == ['image', 'inspect']:
    raise SystemExit(1 if args[-1] == os.environ.get('TESTPILOT_FAKE_MISSING') else 0)
if args and args[0] == 'run' and '/install/generate_config.py' in args:
    environment = dict(os.environ, TESTPILOT_CONFIG_ROOT=str(Path.cwd()))
    for index, arg in enumerate(args):
        if arg == '-e':
            key, value = args[index + 1].split('=', 1)
            environment[key] = value
    raise SystemExit(subprocess.run([sys.executable, 'generate_config.py'], env=environment).returncode)
if args and args[0] == 'compose' and args[-1] == 'stop':
    if '--env-file' not in args:
        raise SystemExit(11)
    configuration = Path(args[args.index('--env-file') + 1])
    if 'TESTPILOT_TLS_DIR=' not in configuration.read_text():
        raise SystemExit(12)
    if 'deploy/client.yaml' in args and os.environ.get('TESTPILOT_FAKE_STOP_FAIL'):
        raise SystemExit(7)
raise SystemExit(0)
'''


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="testpilot-release-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "package"
        shutil.copytree(ROOT / "deploy/package", self.package, ignore=shutil.ignore_patterns("*.env", "data", "client", "tls"))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text(FAKE_DOCKER.replace("#!PYTHON", "#!" + sys.executable, 1))
        fake.chmod(0o755)
        self.log = self.root / "docker.jsonl"
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], TESTPILOT_FAKE_LOG=str(self.log))
        payload = b"fake image archive: Docker is never invoked"
        (self.package / "images.tar.gz").write_bytes(payload)
        (self.package / "images.sha256").write_text(hashlib.sha256(payload).hexdigest() + "  images.tar.gz\n")

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def install(self, *arguments, missing=""):
        env = dict(self.environment, TESTPILOT_FAKE_MISSING=missing)
        return subprocess.run(["bash", "install.sh", "--offline", *arguments], cwd=self.package, env=env, capture_output=True, text=True, timeout=30)

    def test_base_manifest_covers_all_fixed_compose_images(self):
        images = release.required_images(release.image_defaults())
        self.assertIn("redis:7.4-alpine", images)
        self.assertIn("qdrant/qdrant:v1.16.3", images)
        compose = (ROOT / "deploy/package/compose.yaml").read_text()
        for line in compose.splitlines():
            if line.strip().startswith("image: "):
                value = line.split("image: ", 1)[1]
                if "${" not in value:
                    self.assertIn(value, images)

    def test_stop_loads_tls_configuration_and_attempts_both_groups(self):
        project = self.root / 'stop-project'
        (project / 'scripts').mkdir(parents=True)
        (project / 'deploy').mkdir()
        shutil.copy2(ROOT / 'scripts/stop.sh', project / 'scripts/stop.sh')
        configuration = project / 'deploy/platform.env'
        for configured, client_failure in [(False, False), (True, False), (True, True)]:
            with self.subTest(configured=configured, client_failure=client_failure):
                self.log.unlink(missing_ok=True)
                if configured:
                    configuration.write_text('TESTPILOT_TLS_DIR=/tmp/testpilot-test-tls\n')
                else:
                    configuration.unlink(missing_ok=True)
                env = dict(self.environment)
                env.pop('TESTPILOT_TLS_DIR', None)
                if client_failure:
                    env['TESTPILOT_FAKE_STOP_FAIL'] = '1'
                result = subprocess.run(['bash', 'scripts/stop.sh'], cwd=project, env=env,
                                        capture_output=True, text=True, timeout=10)
                commands = self.commands()
                self.assertEqual(result.returncode, 7 if client_failure else 0)
                if not configured:
                    self.assertEqual(commands, [])
                    continue
                self.assertEqual(len(commands), 2)
                self.assertTrue(all(command[:3] == ['compose', '--env-file', 'deploy/platform.env'] for command in commands))
                self.assertIn('deploy/client.yaml', commands[0])
                self.assertIn('deploy/compose.yaml', commands[1])

    def test_optional_images_join_the_same_preflight_manifest(self):
        images = release.required_images(release.image_defaults(), performance=True, android=True)
        self.assertEqual(len(images), 6)
        self.assertIn("testpilot-jmeter:0.1", images)
        self.assertIn("testpilot-appium:0.1", images)

    def test_missing_redis_or_qdrant_stops_before_configuration_and_never_pulls(self):
        for image in ["redis:7.4-alpine", "qdrant/qdrant:v1.16.3"]:
            with self.subTest(image=image):
                self.log.unlink(missing_ok=True)
                result = self.install(missing=image)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(image, result.stderr)
                commands = self.commands()
                self.assertFalse(any(command[0] in {"pull", "run"} or "up" in command for command in commands))

    def test_missing_optional_performance_image_never_falls_back_to_pull(self):
        result = self.install("--performance", missing="testpilot-jmeter:0.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("testpilot-jmeter:0.1", result.stderr)
        self.assertFalse(any(command[0] == "pull" or "up" in command for command in self.commands()))

    def test_complete_offline_install_disables_all_implicit_pulls(self):
        result = self.install("--performance")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertFalse(any(command[0] == "pull" for command in commands))
        inspected = [command[-1] for command in commands if command[:2] == ["image", "inspect"]]
        self.assertEqual(set(inspected), set(release.required_images(release.image_defaults(), performance=True)))
        runs = [command for command in commands if command[0] == "run"]
        self.assertEqual(len(runs), 2)
        self.assertTrue(all("--pull=never" in command for command in runs))
        starts = [command for command in commands if command[0] == "compose" and "up" in command]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][starts[0].index("--pull") + 1], "never")
        self.assertTrue((self.package / "install.env").is_file())

    def test_manifest_is_parsed_instead_of_executed(self):
        (self.package / "images.conf").write_text("runtime=$(touch malicious-file)\n")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.package / "malicious-file").exists())
        self.assertEqual(self.commands(), [])

    def test_bundle_uses_public_readme_and_only_allowlisted_install_files(self):
        project = self.root / "project"
        templates = project / "deploy/package"
        shutil.copytree(self.package, templates)
        (templates / "api.env").write_text("PRIVATE_SECRET=do-not-package\n")
        (project / "docs").mkdir()
        (project / "docs/双模式部署与安装.md").write_text("PRIVATE LEARNING NOTES")
        (project / "README.md").write_text("PUBLIC INSTALLATION GUIDE")
        for name in release.PUBLIC_FILES:
            shutil.copy2(ROOT / name, project / name)
            (templates / name).write_text("WRONG LICENSE FROM INSTALL TEMPLATE")
        (project / "web").mkdir()
        output = project / "releases/check"
        with patch.object(release, "ROOT", project), patch.object(release.subprocess, "run"):
            release.build(output)
        self.assertEqual((output / "使用说明.md").read_text(), "PUBLIC INSTALLATION GUIDE")
        with tarfile.open(str(output) + ".tar.gz") as archive:
            names = set(archive.getnames())
        self.assertNotIn("TestPilot/api.env", names)
        self.assertNotIn("TestPilot/images.tar.gz", names)
        self.assertNotIn("TestPilot/images.sha256", names)
        self.assertEqual({path.name for path in output.iterdir()}, set(release.INSTALL_FILES) | set(release.PUBLIC_FILES) | {"使用说明.md"})
        for name in release.PUBLIC_FILES:
            self.assertIn("TestPilot/" + name, names)
            self.assertEqual((output / name).read_bytes(), (ROOT / name).read_bytes())

    def test_refresh_does_not_archive_unexpected_existing_files(self):
        project = self.root / "refresh-project"
        shutil.copytree(self.package, project / "deploy/package")
        (project / "README.md").write_text("PUBLIC INSTALLATION GUIDE")
        for name in release.PUBLIC_FILES:shutil.copy2(ROOT / name, project / name)
        (project / "web").mkdir()
        output = project / "releases/check"
        output.mkdir(parents=True)
        (output / "personal-notes.env").write_text("PRIVATE_SECRET=do-not-package\n")
        (output / "images.tar.gz").write_bytes(b"stale image archive from a previous build")
        with patch.object(release, "ROOT", project), patch.object(release.subprocess, "run"):
            release.build(output, refresh=True)
        with tarfile.open(str(output) + ".tar.gz") as archive:
            names = set(archive.getnames())
        self.assertNotIn("TestPilot/personal-notes.env", names)
        self.assertNotIn("TestPilot/images.tar.gz", names)

    def test_offline_export_contains_every_required_image_and_a_matching_digest(self):
        project = self.root / "offline-project"
        shutil.copytree(self.package, project / "deploy/package")
        (project / "README.md").write_text("PUBLIC INSTALLATION GUIDE")
        for name in release.PUBLIC_FILES:shutil.copy2(ROOT / name, project / name)
        (project / "web").mkdir()
        output = project / "releases/check"
        exported = []
        real_popen, real_run = subprocess.Popen, subprocess.run

        def fake_popen(arguments, **kwargs):
            if arguments[:3] == ["docker", "image", "save"]:
                exported.extend(arguments[3:])
                arguments = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'fake exported images')"]
            return real_popen(arguments, **kwargs)

        def fake_run(arguments, **kwargs):
            return real_run(arguments, **kwargs) if arguments[0] == "gzip" else subprocess.CompletedProcess(arguments, 0)

        with patch.object(release, "ROOT", project), patch.object(release.subprocess, "run", side_effect=fake_run), patch.object(release.subprocess, "Popen", side_effect=fake_popen):
            release.build(output, offline=True, performance=True, android=True)
        self.assertEqual(set(exported), set(release.required_images(release.image_defaults(), performance=True, android=True)))
        self.assertEqual(gzip.decompress((output / "images.tar.gz").read_bytes()), b"fake exported images")
        digest = hashlib.sha256((output / "images.tar.gz").read_bytes()).hexdigest()
        self.assertEqual((output / "images.sha256").read_text(), digest + "  images.tar.gz\n")
        with tarfile.open(str(output) + ".tar.gz") as archive:
            names = set(archive.getnames())
            for name in release.PUBLIC_FILES:
                self.assertEqual(archive.extractfile("TestPilot/" + name).read(), (ROOT / name).read_bytes())
        self.assertIn("TestPilot/images.tar.gz", names)
        self.assertIn("TestPilot/images.sha256", names)


if __name__ == "__main__":
    unittest.main()
