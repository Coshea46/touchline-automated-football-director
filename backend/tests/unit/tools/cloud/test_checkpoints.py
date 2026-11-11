"""Offline tests for unit tools cloud test_checkpoints."""


import hashlib
from types import SimpleNamespace
import pytest
from touchline.tools.cloud.checkpoints import install_checkpoint


def fixture(tmp_path):
    path = tmp_path / "classifier.pt"
    path.write_bytes(b"installer fixture, not trained weights")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    audit = {"bundle_sha256": digest, "model_version": "v1/classifier.pt",
             "schema": "matchpulse-215-v1", "input_shape": [75, 215], "audio_required": True,
             "architecture": {"kind": "gru", "hidden_size": 64}}
    uploads, lookups, cancelled, stored = [], [], [], {}
    failure = []

    class Batch:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def put_file(self, stream, remote):
            if remote in stored:
                raise FileExistsError("Immutable version exists")
            stored[remote] = stream.read()

    class Volume:
        @staticmethod
        def from_name(name, **kwargs):
            assert name == "matchpulse-inference-models"
            assert kwargs == {"create_if_missing": True}

            def batch_upload(**kwargs):
                uploads.append(kwargs)
                return Batch()

            return SimpleNamespace(batch_upload=batch_upload)

    class Handle:
        def get(self, timeout):
            assert timeout <= 180
            if failure:
                raise failure[0]
            return audit

        def cancel(self, **kwargs):
            cancelled.append(kwargs)

    class Cls:
        @staticmethod
        def from_name(app, name):
            assert app == "matchpulse-inference" and name == "ClassifierWorker"
            lookups.append((app, name))

            def worker(**kwargs):
                assert kwargs == {"checkpoint_name": "v1/classifier.pt"}
                return SimpleNamespace(ready=SimpleNamespace(spawn=Handle))

            return worker

    sdk = SimpleNamespace(Volume=Volume, Cls=Cls)
    return SimpleNamespace(path=path, digest=digest, sdk=sdk, uploads=uploads,
                           lookups=lookups, cancelled=cancelled, stored=stored, audit=audit, failure=failure)


def test_installs_verified_bytes_immutably_into_only_inference_volume_and_cpu_readiness(tmp_path):
    f = fixture(tmp_path)
    result = install_checkpoint(f.path, version="v1", expected_sha256=f.digest, allow_cloud=True, sdk=f.sdk)
    assert result["readiness_verified"] and result["installed"]
    assert f.uploads == [{"force": False}]
    assert f.stored == {"/v1/classifier.pt": f.path.read_bytes()}
    assert f.lookups == [("matchpulse-inference", "ClassifierWorker")]
    with pytest.raises(FileExistsError):
        install_checkpoint(f.path, version="v1", expected_sha256=f.digest, allow_cloud=True, sdk=f.sdk)


def test_verify_only_rechecks_cpu_without_overwriting_existing_version(tmp_path):
    f = fixture(tmp_path)
    result = install_checkpoint(f.path, version="v1", expected_sha256=f.digest,
                                allow_cloud=True, verify_only=True, sdk=f.sdk)
    assert not result["installed"] and not f.uploads


@pytest.mark.parametrize("version", ["../v1", "/v1", "v1/sub", "..", "v1\\sub"])
def test_installer_rejects_unsafe_versions_before_sdk_access(tmp_path, version):
    f = fixture(tmp_path)
    with pytest.raises(ValueError, match="directory"):
        install_checkpoint(f.path, version=version, expected_sha256=f.digest, allow_cloud=True, sdk=object())


def test_installer_requires_explicit_authorization_and_verified_digest_before_upload(tmp_path):
    f = fixture(tmp_path)
    with pytest.raises(ValueError, match="allow-cloud"):
        install_checkpoint(f.path, version="v1", expected_sha256=f.digest, sdk=object())
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        install_checkpoint(f.path, version="v1", expected_sha256="0" * 64, allow_cloud=True, sdk=object())
    assert not f.uploads and not f.lookups


def test_installer_cancels_only_owned_cpu_readiness_input_on_timeout(tmp_path):
    f = fixture(tmp_path)
    f.failure.append(TimeoutError())
    with pytest.raises(TimeoutError):
        install_checkpoint(f.path, version="v1", expected_sha256=f.digest, allow_cloud=True, sdk=f.sdk)
    assert f.cancelled == [{"terminate_containers": False}]


def test_installer_rejects_remote_digest_mismatch_and_old_four_key_readiness(tmp_path):
    f = fixture(tmp_path)
    f.audit.pop("bundle_sha256")
    with pytest.raises(ValueError, match="CPU readiness"):
        install_checkpoint(f.path, version="v1", expected_sha256=f.digest, allow_cloud=True, sdk=f.sdk)
