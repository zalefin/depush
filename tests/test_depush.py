"""
test_depush.py — tests for depush.py

Unit tests run without any external services.
Integration tests require the docker-compose services to be running:
  docker compose up -d

Run only unit tests:
  pytest test_depush.py -m "not integration"

Run all tests (requires Docker containers):
  pytest test_depush.py
"""

import argparse
import os
import sys
import textwrap
from pathlib import Path

import pytest

from depush import depush

# ---------------------------------------------------------------------------
# Constants matching docker-compose services
# ---------------------------------------------------------------------------

MINIO_ENDPOINT = "http://localhost:9000"
MINIO_ACCESS_KEY = "admin"
MINIO_SECRET_KEY = "password"
MINIO_REGION = "us-east-1"
MINIO_BUCKET = "test-depush"

SSH_HOST = "localhost"
SSH_PORT = 2222
SSH_USER = "admin"
SSH_PASSWORD = "changeme"
SSH_DEPLOY_ROOT = "/tmp/depush-test-deployments"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_codebase(tmp_path):
    """A temporary codebase directory with a version file and a few files."""
    cb = tmp_path / "codebase"
    cb.mkdir()
    (cb / "version").write_text("1.2.3\n")
    (cb / "main.py").write_text("print('hello')\n")
    sub = cb / "lib"
    sub.mkdir()
    (sub / "utils.py").write_text("x = 1\n")
    return cb


@pytest.fixture()
def cfg_file(tmp_path):
    """Factory that writes a YAML config file and returns its path."""

    def _write(content: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent(content))
        return p

    return _write


def _make_args(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace with sensible defaults, overridden by kwargs."""
    defaults = dict(
        target="local",
        prefix="mylib",
        codebase_dir=".",
        dry_run=False,
        config=None,
        local_dest=None,
        s3_bucket=None,
        s3_endpoint=None,
        s3_region="us-east-1",
        s3_profile=None,
        s3_access_key=None,
        s3_secret_key=None,
        ssh_host=None,
        ssh_port=22,
        ssh_user="admin",
        ssh_password=None,
        ssh_key_file=None,
        ssh_deploy_root="/deployments",
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# load_yaml_config
# ---------------------------------------------------------------------------


class TestLoadYamlConfig:
    def test_top_level_keys(self, cfg_file):
        p = cfg_file(
            """\
            target: s3
            prefix: myapp
            codebase_dir: ./cb
            dry_run: true
            """
        )
        cfg = depush.load_yaml_config(str(p))
        assert cfg["target"] == "s3"
        assert cfg["prefix"] == "myapp"
        assert cfg["codebase_dir"] == "./cb"
        assert cfg["dry_run"] is True

    def test_s3_section(self, cfg_file):
        p = cfg_file(
            """\
            s3:
              bucket: my-bucket
              endpoint: http://minio:9000
              region: eu-west-1
              access_key: AKID
              secret_key: SECRET
            """
        )
        cfg = depush.load_yaml_config(str(p))
        assert cfg["s3_bucket"] == "my-bucket"
        assert cfg["s3_endpoint"] == "http://minio:9000"
        assert cfg["s3_region"] == "eu-west-1"
        assert cfg["s3_access_key"] == "AKID"
        assert cfg["s3_secret_key"] == "SECRET"

    def test_s3_profile_key(self, cfg_file):
        p = cfg_file(
            """\
            s3:
              bucket: my-bucket
              profile: my-aws-profile
            """
        )
        cfg = depush.load_yaml_config(str(p))
        assert cfg["s3_profile"] == "my-aws-profile"

    def test_ssh_section(self, cfg_file):
        p = cfg_file(
            """\
            ssh:
              host: prod.example.com
              port: 2222
              user: deploy
              password: secret
              key_file: /home/user/.ssh/id_rsa
              deploy_root: /srv/releases
            """
        )
        cfg = depush.load_yaml_config(str(p))
        assert cfg["ssh_host"] == "prod.example.com"
        assert cfg["ssh_port"] == 2222
        assert cfg["ssh_user"] == "deploy"
        assert cfg["ssh_password"] == "secret"
        assert cfg["ssh_key_file"] == "/home/user/.ssh/id_rsa"
        assert cfg["ssh_deploy_root"] == "/srv/releases"

    def test_local_section(self, cfg_file):
        p = cfg_file(
            """\
            local:
              dest: /srv/dist
            """
        )
        cfg = depush.load_yaml_config(str(p))
        assert cfg["local_dest"] == "/srv/dist"

    def test_empty_file_returns_empty_dict(self, cfg_file):
        p = cfg_file("")
        assert depush.load_yaml_config(str(p)) == {}

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            depush.load_yaml_config(str(tmp_path / "nonexistent.yaml"))

    def test_unknown_keys_ignored(self, cfg_file):
        p = cfg_file(
            """\
            unknown_top_key: value
            s3:
              mystery_field: ignored
              bucket: real-bucket
            """
        )
        cfg = depush.load_yaml_config(str(p))
        assert "unknown_top_key" not in cfg
        assert cfg["s3_bucket"] == "real-bucket"


# ---------------------------------------------------------------------------
# resolve_defaults
# ---------------------------------------------------------------------------


class TestResolveDefaults:
    def test_hardcoded_defaults_present(self):
        merged = depush.resolve_defaults({})
        for k, v in depush.DEFAULTS.items():
            assert merged[k] == v

    def test_yaml_overrides_hardcoded(self):
        merged = depush.resolve_defaults({"s3_region": "ap-southeast-1"})
        assert merged["s3_region"] == "ap-southeast-1"

    def test_s3_profile_env_var(self, monkeypatch):
        monkeypatch.setenv("S3_PROFILE", "ci-profile")
        merged = depush.resolve_defaults({})
        assert merged["s3_profile"] == "ci-profile"

    def test_aws_profile_fallback(self, monkeypatch):
        monkeypatch.setenv("AWS_PROFILE", "default-aws-profile")
        merged = depush.resolve_defaults({})
        assert merged["s3_profile"] == "default-aws-profile"

    def test_s3_profile_takes_precedence_over_aws_profile(self, monkeypatch):
        monkeypatch.setenv("S3_PROFILE", "explicit-profile")
        monkeypatch.setenv("AWS_PROFILE", "default-aws-profile")
        merged = depush.resolve_defaults({})
        assert merged["s3_profile"] == "explicit-profile"

    def test_env_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("S3_REGION", "eu-central-1")
        merged = depush.resolve_defaults({"s3_region": "ap-southeast-1"})
        assert merged["s3_region"] == "eu-central-1"

    def test_env_bool_dry_run(self, monkeypatch):
        for truthy in ("1", "true", "yes", "TRUE", "YES"):
            monkeypatch.setenv("DEPLOY_DRY_RUN", truthy)
            assert depush.resolve_defaults({})["dry_run"] is True

        for falsy in ("0", "false", "no"):
            monkeypatch.setenv("DEPLOY_DRY_RUN", falsy)
            assert depush.resolve_defaults({})["dry_run"] is False

    def test_env_ssh_port_int(self, monkeypatch):
        monkeypatch.setenv("SSH_PORT", "2222")
        assert depush.resolve_defaults({})["ssh_port"] == 2222

    def test_env_ssh_port_invalid_exits(self, monkeypatch):
        monkeypatch.setenv("SSH_PORT", "not-a-number")
        with pytest.raises(SystemExit):
            depush.resolve_defaults({})

    def test_env_not_set_does_not_override(self, monkeypatch):
        monkeypatch.delenv("SSH_PORT", raising=False)
        merged = depush.resolve_defaults({"ssh_port": 9999})
        assert merged["ssh_port"] == 9999


# ---------------------------------------------------------------------------
# read_version
# ---------------------------------------------------------------------------


class TestReadVersion:
    def test_reads_version(self, tmp_path):
        (tmp_path / "version").write_text("2.0.0\n")
        assert depush.read_version(tmp_path) == "2.0.0"

    def test_strips_whitespace(self, tmp_path):
        (tmp_path / "version").write_text("  v1.5.0  \n")
        assert depush.read_version(tmp_path) == "v1.5.0"

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            depush.read_version(tmp_path)


# ---------------------------------------------------------------------------
# collect_files
# ---------------------------------------------------------------------------


class TestCollectFiles:
    def test_returns_only_files(self, tmp_codebase):
        files = depush.collect_files(tmp_codebase)
        assert all(f.is_file() for f in files)

    def test_finds_nested_files(self, tmp_codebase):
        names = {f.name for f in depush.collect_files(tmp_codebase)}
        assert "utils.py" in names

    def test_excludes_git(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]\n")
        (tmp_path / "real.py").write_text("x = 1\n")
        files = depush.collect_files(tmp_path)
        assert all(".git" not in str(f) for f in files)
        assert any(f.name == "real.py" for f in files)

    def test_sorted_output(self, tmp_codebase):
        files = depush.collect_files(tmp_codebase)
        assert files == sorted(files)

    def test_empty_dir_returns_empty(self, tmp_path):
        assert depush.collect_files(tmp_path) == []

    def test_excludes_depush_yaml(self, tmp_codebase):
        (tmp_codebase / "depush.yaml").write_text("target: s3\n")
        names = {f.name for f in depush.collect_files(tmp_codebase)}
        assert "depush.yaml" not in names

    def test_depush_yaml_in_subdir_not_excluded(self, tmp_codebase):
        # Only the root-level depush.yaml is excluded; nested ones are kept
        sub = tmp_codebase / "conf"
        sub.mkdir()
        (sub / "depush.yaml").write_text("target: s3\n")
        names = {f.name for f in depush.collect_files(tmp_codebase)}
        assert "depush.yaml" in names


# ---------------------------------------------------------------------------
# load_ignore_spec / .depushignore
# ---------------------------------------------------------------------------


class TestLoadIgnoreSpec:
    def test_returns_none_when_no_file(self, tmp_path):
        assert depush.load_ignore_spec(tmp_path) is None

    def test_returns_spec_when_file_exists(self, tmp_path):
        (tmp_path / ".depushignore").write_text("*.log\n")
        spec = depush.load_ignore_spec(tmp_path)
        assert spec is not None

    def test_ignored_file_not_in_collect(self, tmp_codebase):
        (tmp_codebase / ".depushignore").write_text("*.log\n")
        (tmp_codebase / "debug.log").write_text("log data\n")
        spec = depush.load_ignore_spec(tmp_codebase)
        names = {f.name for f in depush.collect_files(tmp_codebase, spec)}
        assert "debug.log" not in names

    def test_non_ignored_file_still_included(self, tmp_codebase):
        (tmp_codebase / ".depushignore").write_text("*.log\n")
        spec = depush.load_ignore_spec(tmp_codebase)
        names = {f.name for f in depush.collect_files(tmp_codebase, spec)}
        assert "main.py" in names

    def test_depushignore_itself_excluded(self, tmp_codebase):
        (tmp_codebase / ".depushignore").write_text("*.log\n")
        spec = depush.load_ignore_spec(tmp_codebase)
        names = {f.name for f in depush.collect_files(tmp_codebase, spec)}
        assert ".depushignore" not in names

    def test_directory_pattern(self, tmp_codebase):
        (tmp_codebase / ".depushignore").write_text("lib/\n")
        spec = depush.load_ignore_spec(tmp_codebase)
        names = {f.name for f in depush.collect_files(tmp_codebase, spec)}
        assert "utils.py" not in names
        assert "main.py" in names

    def test_negation_pattern(self, tmp_codebase):
        (tmp_codebase / ".depushignore").write_text("*.py\n!main.py\n")
        spec = depush.load_ignore_spec(tmp_codebase)
        names = {f.name for f in depush.collect_files(tmp_codebase, spec)}
        assert "main.py" in names
        assert "utils.py" not in names


class TestDeployLocalIgnore:
    def test_ignored_stale_file_not_deleted(self, tmp_codebase, tmp_path):
        dest = tmp_path / "dist"
        deploy_path = "mylib/1.2.3"
        target_dir = dest / deploy_path
        target_dir.mkdir(parents=True)
        # Place a file that matches an ignore pattern at the destination
        (target_dir / "secrets.env").write_text("KEY=value\n")
        (tmp_codebase / ".depushignore").write_text("*.env\n")

        args = _make_args(
            target="local", prefix="mylib", local_dest=str(dest), dry_run=False
        )
        depush.deploy_local(args, tmp_codebase, deploy_path)

        assert (target_dir / "secrets.env").exists()

    def test_ignored_file_not_uploaded(self, tmp_codebase, tmp_path):
        (tmp_codebase / ".depushignore").write_text("*.log\n")
        (tmp_codebase / "debug.log").write_text("log\n")
        dest = tmp_path / "dist"
        args = _make_args(
            target="local", prefix="mylib", local_dest=str(dest), dry_run=False
        )
        depush.deploy_local(args, tmp_codebase, "mylib/1.2.3")
        assert not (dest / "mylib" / "1.2.3" / "debug.log").exists()


# ---------------------------------------------------------------------------


class TestValidate:
    def test_missing_target_exits(self):
        args = _make_args(target=None)
        with pytest.raises(SystemExit):
            depush.validate(args)

    def test_missing_prefix_exits(self):
        args = _make_args(prefix=None)
        with pytest.raises(SystemExit):
            depush.validate(args)

    def test_s3_missing_bucket_exits(self):
        args = _make_args(target="s3", s3_bucket=None)
        with pytest.raises(SystemExit):
            depush.validate(args)

    def test_s3_with_bucket_ok(self):
        args = _make_args(target="s3", s3_bucket="my-bucket")
        depush.validate(args)  # should not raise

    def test_ssh_missing_host_exits(self):
        args = _make_args(target="ssh", ssh_host=None)
        with pytest.raises(SystemExit):
            depush.validate(args)

    def test_ssh_with_host_ok(self):
        args = _make_args(target="ssh", ssh_host="prod.example.com")
        depush.validate(args)

    def test_local_missing_dest_exits(self):
        args = _make_args(target="local", local_dest=None)
        with pytest.raises(SystemExit):
            depush.validate(args)

    def test_local_with_dest_ok(self):
        args = _make_args(target="local", local_dest="/tmp/dist")
        depush.validate(args)


# ---------------------------------------------------------------------------
# deploy_local — unit tests (no containers needed)
# ---------------------------------------------------------------------------


class TestDeployLocal:
    def test_creates_files_at_destination(self, tmp_codebase, tmp_path):
        dest = tmp_path / "dist"
        args = _make_args(
            target="local", prefix="mylib", local_dest=str(dest), dry_run=False
        )
        depush.deploy_local(args, tmp_codebase, "mylib/1.2.3")
        assert (dest / "mylib" / "1.2.3" / "main.py").exists()
        assert (dest / "mylib" / "1.2.3" / "lib" / "utils.py").exists()

    def test_dry_run_does_not_create_files(self, tmp_codebase, tmp_path):
        dest = tmp_path / "dist"
        args = _make_args(
            target="local", prefix="mylib", local_dest=str(dest), dry_run=True
        )
        depush.deploy_local(args, tmp_codebase, "mylib/1.2.3")
        assert not dest.exists()

    def test_dry_run_prints_paths(self, tmp_codebase, tmp_path, capsys):
        dest = tmp_path / "dist"
        args = _make_args(
            target="local", prefix="mylib", local_dest=str(dest), dry_run=True
        )
        depush.deploy_local(args, tmp_codebase, "mylib/1.2.3")
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "main.py" in out

    def test_deletes_stale_files(self, tmp_codebase, tmp_path):
        dest = tmp_path / "dist"
        deploy_path = "mylib/1.2.3"
        target_dir = dest / deploy_path
        target_dir.mkdir(parents=True)
        stale = target_dir / "stale_old_file.py"
        stale.write_text("old\n")

        args = _make_args(
            target="local", prefix="mylib", local_dest=str(dest), dry_run=False
        )
        depush.deploy_local(args, tmp_codebase, deploy_path)
        assert not stale.exists()

    def test_dry_run_reports_stale_deletions(self, tmp_codebase, tmp_path, capsys):
        dest = tmp_path / "dist"
        deploy_path = "mylib/1.2.3"
        target_dir = dest / deploy_path
        target_dir.mkdir(parents=True)
        (target_dir / "stale.py").write_text("old\n")

        args = _make_args(
            target="local", prefix="mylib", local_dest=str(dest), dry_run=True
        )
        depush.deploy_local(args, tmp_codebase, deploy_path)
        out = capsys.readouterr().out
        assert "stale.py" in out

    def test_exits_on_empty_codebase(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        args = _make_args(
            target="local", local_dest=str(tmp_path / "dist"), dry_run=False
        )
        with pytest.raises(SystemExit):
            depush.deploy_local(args, empty, "mylib/1.0.0")

    def test_summary_line_printed(self, tmp_codebase, tmp_path, capsys):
        dest = tmp_path / "dist"
        args = _make_args(
            target="local", prefix="mylib", local_dest=str(dest), dry_run=False
        )
        depush.deploy_local(args, tmp_codebase, "mylib/1.2.3")
        out = capsys.readouterr().out
        assert "Deployed" in out
        assert "3 file(s)" in out


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_parses_target(self):
        parser = depush.build_parser()
        args = parser.parse_args(["--target", "s3", "--prefix", "p"])
        assert args.target == "s3"

    def test_parses_dry_run_flag(self):
        parser = depush.build_parser()
        args = parser.parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_invalid_target_exits(self):
        parser = depush.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--target", "ftp"])


# ---------------------------------------------------------------------------
# deploy_s3 — unit tests (boto3 mocked)
# ---------------------------------------------------------------------------


class TestDeployS3Unit:
    def test_profile_passed_to_session(self, tmp_codebase, monkeypatch):
        """deploy_s3 should pass s3_profile as profile_name to boto3.Session."""
        import unittest.mock as mock

        mock_s3 = mock.MagicMock()
        mock_session = mock.MagicMock()
        mock_session.client.return_value = mock_s3
        mock_s3.get_paginator.return_value.paginate.return_value = []

        with mock.patch("boto3.Session", return_value=mock_session) as mock_session_cls:
            args = _make_args(
                target="s3",
                s3_bucket="my-bucket",
                s3_profile="my-aws-profile",
                dry_run=False,
            )
            depush.deploy_s3(args, tmp_codebase, "mylib/1.2.3")

        mock_session_cls.assert_called_once_with(
            profile_name="my-aws-profile",
            aws_access_key_id=None,
            aws_secret_access_key=None,
            region_name="us-east-1",
        )

    def test_no_profile_uses_none(self, tmp_codebase):
        """When no profile is specified, profile_name should be None (use default chain)."""
        import unittest.mock as mock

        mock_s3 = mock.MagicMock()
        mock_session = mock.MagicMock()
        mock_session.client.return_value = mock_s3
        mock_s3.get_paginator.return_value.paginate.return_value = []

        with mock.patch("boto3.Session", return_value=mock_session) as mock_session_cls:
            args = _make_args(
                target="s3",
                s3_bucket="my-bucket",
                dry_run=False,
            )
            depush.deploy_s3(args, tmp_codebase, "mylib/1.2.3")

        mock_session_cls.assert_called_once_with(
            profile_name=None,
            aws_access_key_id=None,
            aws_secret_access_key=None,
            region_name="us-east-1",
        )


# ---------------------------------------------------------------------------
# Integration — MinIO (S3)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDeployS3Integration:
    @pytest.fixture(autouse=True)
    def minio_bucket(self):
        """Create the test bucket before each test and remove it after."""
        boto3 = pytest.importorskip("boto3")
        from botocore.client import Config

        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id=MINIO_ACCESS_KEY,
            aws_secret_access_key=MINIO_SECRET_KEY,
            region_name=MINIO_REGION,
            config=Config(signature_version="s3v4"),
        )
        try:
            s3.create_bucket(Bucket=MINIO_BUCKET)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass

        yield s3

        # Cleanup: delete all objects and the bucket
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=MINIO_BUCKET):
            for obj in page.get("Contents", []):
                s3.delete_object(Bucket=MINIO_BUCKET, Key=obj["Key"])
        s3.delete_bucket(Bucket=MINIO_BUCKET)

    def test_files_uploaded(self, tmp_codebase, minio_bucket):
        args = _make_args(
            target="s3",
            prefix="mylib",
            s3_bucket=MINIO_BUCKET,
            s3_endpoint=MINIO_ENDPOINT,
            s3_region=MINIO_REGION,
            s3_access_key=MINIO_ACCESS_KEY,
            s3_secret_key=MINIO_SECRET_KEY,
            dry_run=False,
        )
        depush.deploy_s3(args, tmp_codebase, "mylib/1.2.3")

        keys = [
            obj["Key"]
            for page in minio_bucket.get_paginator("list_objects_v2").paginate(
                Bucket=MINIO_BUCKET, Prefix="mylib/1.2.3/"
            )
            for obj in page.get("Contents", [])
        ]
        assert "mylib/1.2.3/main.py" in keys
        assert "mylib/1.2.3/lib/utils.py" in keys

    def test_stale_objects_deleted(self, tmp_codebase, minio_bucket):
        # Pre-populate a stale object
        minio_bucket.put_object(
            Bucket=MINIO_BUCKET,
            Key="mylib/1.2.3/old_artifact.py",
            Body=b"stale",
        )
        args = _make_args(
            target="s3",
            prefix="mylib",
            s3_bucket=MINIO_BUCKET,
            s3_endpoint=MINIO_ENDPOINT,
            s3_region=MINIO_REGION,
            s3_access_key=MINIO_ACCESS_KEY,
            s3_secret_key=MINIO_SECRET_KEY,
            dry_run=False,
        )
        depush.deploy_s3(args, tmp_codebase, "mylib/1.2.3")

        remaining = [
            obj["Key"]
            for page in minio_bucket.get_paginator("list_objects_v2").paginate(
                Bucket=MINIO_BUCKET, Prefix="mylib/1.2.3/"
            )
            for obj in page.get("Contents", [])
        ]
        assert "mylib/1.2.3/old_artifact.py" not in remaining

    def test_dry_run_uploads_nothing(self, tmp_codebase, minio_bucket):
        args = _make_args(
            target="s3",
            prefix="mylib",
            s3_bucket=MINIO_BUCKET,
            s3_endpoint=MINIO_ENDPOINT,
            s3_region=MINIO_REGION,
            s3_access_key=MINIO_ACCESS_KEY,
            s3_secret_key=MINIO_SECRET_KEY,
            dry_run=True,
        )
        depush.deploy_s3(args, tmp_codebase, "mylib/1.2.3")

        pages = list(
            minio_bucket.get_paginator("list_objects_v2").paginate(
                Bucket=MINIO_BUCKET, Prefix="mylib/1.2.3/"
            )
        )
        keys = [obj["Key"] for page in pages for obj in page.get("Contents", [])]
        assert keys == []

    def test_idempotent_redeploy(self, tmp_codebase, minio_bucket):
        args = _make_args(
            target="s3",
            prefix="mylib",
            s3_bucket=MINIO_BUCKET,
            s3_endpoint=MINIO_ENDPOINT,
            s3_region=MINIO_REGION,
            s3_access_key=MINIO_ACCESS_KEY,
            s3_secret_key=MINIO_SECRET_KEY,
            dry_run=False,
        )
        depush.deploy_s3(args, tmp_codebase, "mylib/1.2.3")
        depush.deploy_s3(
            args, tmp_codebase, "mylib/1.2.3"
        )  # second deploy should not error

        keys = [
            obj["Key"]
            for page in minio_bucket.get_paginator("list_objects_v2").paginate(
                Bucket=MINIO_BUCKET, Prefix="mylib/1.2.3/"
            )
            for obj in page.get("Contents", [])
        ]
        assert len(keys) == 3  # main.py + lib/utils.py + version

    def test_ignored_file_not_uploaded(self, tmp_codebase, minio_bucket):
        (tmp_codebase / ".depushignore").write_text("*.py\n")
        args = _make_args(
            target="s3",
            prefix="mylib",
            s3_bucket=MINIO_BUCKET,
            s3_endpoint=MINIO_ENDPOINT,
            s3_region=MINIO_REGION,
            s3_access_key=MINIO_ACCESS_KEY,
            s3_secret_key=MINIO_SECRET_KEY,
            dry_run=False,
        )
        depush.deploy_s3(args, tmp_codebase, "mylib/1.2.3")

        keys = [
            obj["Key"]
            for page in minio_bucket.get_paginator("list_objects_v2").paginate(
                Bucket=MINIO_BUCKET, Prefix="mylib/1.2.3/"
            )
            for obj in page.get("Contents", [])
        ]
        assert not any(k.endswith(".py") for k in keys)

    def test_ignored_stale_not_deleted(self, tmp_codebase, minio_bucket):
        # Pre-place a file that matches an ignore pattern
        minio_bucket.put_object(
            Bucket=MINIO_BUCKET,
            Key="mylib/1.2.3/secrets.env",
            Body=b"KEY=value",
        )
        (tmp_codebase / ".depushignore").write_text("*.env\n")
        args = _make_args(
            target="s3",
            prefix="mylib",
            s3_bucket=MINIO_BUCKET,
            s3_endpoint=MINIO_ENDPOINT,
            s3_region=MINIO_REGION,
            s3_access_key=MINIO_ACCESS_KEY,
            s3_secret_key=MINIO_SECRET_KEY,
            dry_run=False,
        )
        depush.deploy_s3(args, tmp_codebase, "mylib/1.2.3")

        keys = [
            obj["Key"]
            for page in minio_bucket.get_paginator("list_objects_v2").paginate(
                Bucket=MINIO_BUCKET, Prefix="mylib/1.2.3/"
            )
            for obj in page.get("Contents", [])
        ]
        assert "mylib/1.2.3/secrets.env" in keys


# ---------------------------------------------------------------------------
# deploy_ssh / _deploy_ssh_rsync / _deploy_ssh_paramiko — unit tests
# ---------------------------------------------------------------------------


class TestDeploySSHUnit:
    from unittest.mock import MagicMock, patch

    def test_rsync_dispatched_when_available_no_password(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_port=22,
            ssh_user="deployer",
            ssh_password=None,
            ssh_key_file=None,
            ssh_deploy_root="/deployments",
            dry_run=False,
        )
        with (
            patch(
                "depush.depush.shutil.which", return_value="/usr/bin/rsync"
            ) as mock_which,
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            mock_which.assert_called_with("rsync")
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "rsync"

    def test_paramiko_dispatched_when_rsync_not_found(self, tmp_codebase):
        from unittest.mock import patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_password=None,
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value=None),
            patch("depush.depush._deploy_ssh_paramiko") as mock_para,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            mock_para.assert_called_once()

    def test_paramiko_dispatched_when_password_set(self, tmp_codebase):
        from unittest.mock import patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_password="secret",
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch("depush.depush._deploy_ssh_paramiko") as mock_para,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            mock_para.assert_called_once()

    def test_rsync_includes_port_in_ssh_opts(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_port=2222,
            ssh_password=None,
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            cmd = mock_run.call_args[0][0]
            ssh_opt = cmd[cmd.index("-e") + 1]
            assert "-p 2222" in ssh_opt

    def test_rsync_includes_key_file_in_ssh_opts(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_port=22,
            ssh_password=None,
            ssh_key_file="/home/user/.ssh/id_rsa",
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            cmd = mock_run.call_args[0][0]
            ssh_opt = cmd[cmd.index("-e") + 1]
            assert "-i /home/user/.ssh/id_rsa" in ssh_opt

    def test_rsync_no_key_file_when_unset(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_password=None,
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            cmd = mock_run.call_args[0][0]
            ssh_opt = cmd[cmd.index("-e") + 1]
            assert "-i " not in ssh_opt

    def test_rsync_includes_exclude_from_when_depushignore_exists(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        (tmp_codebase / ".depushignore").write_text("*.log\n")
        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_password=None,
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            cmd = mock_run.call_args[0][0]
            assert any(arg.startswith("--exclude-from=") for arg in cmd)

    def test_rsync_no_exclude_from_when_no_depushignore(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_password=None,
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            cmd = mock_run.call_args[0][0]
            assert not any(arg.startswith("--exclude-from=") for arg in cmd)

    def test_rsync_nonzero_exit_raises_system_exit(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_password=None,
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch("depush.depush.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            with pytest.raises(SystemExit):
                depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")

    def test_rsync_source_ends_with_slash(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_password=None,
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            cmd = mock_run.call_args[0][0]
            assert cmd[-2].endswith("/")

    def test_rsync_destination_format(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_user="deployer",
            ssh_port=22,
            ssh_password=None,
            ssh_key_file=None,
            ssh_deploy_root="/deployments",
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            cmd = mock_run.call_args[0][0]
            assert cmd[-1] == "deployer@host.example.com:/deployments/mylib/1.0.0/"

    def test_rsync_cmd_always_includes_core_flags(self, tmp_codebase):
        from unittest.mock import MagicMock, patch

        args = _make_args(
            target="ssh",
            ssh_host="host.example.com",
            ssh_password=None,
            ssh_key_file=None,
            dry_run=False,
        )
        with (
            patch("depush.depush.shutil.which", return_value="/usr/bin/rsync"),
            patch(
                "depush.depush.subprocess.run", return_value=MagicMock(returncode=0)
            ) as mock_run,
        ):
            depush.deploy_ssh(args, tmp_codebase, "mylib/1.0.0")
            cmd = mock_run.call_args[0][0]
            assert "--archive" in cmd
            assert "--compress" in cmd
            assert "--delete" in cmd
            assert "--verbose" in cmd
            assert "--exclude=depush.yaml" in cmd
            assert "--exclude=.depushignore" in cmd


# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDeploySSHIntegration:
    @pytest.fixture()
    def ssh_client(self):
        """Return an open paramiko SSHClient connected to the test container."""
        paramiko = pytest.importorskip("paramiko")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=SSH_HOST,
                port=SSH_PORT,
                username=SSH_USER,
                password=SSH_PASSWORD,
            )
        except Exception as exc:
            pytest.skip(f"SSH container not reachable: {exc}")
        yield client
        client.close()

    def _remote_files(self, ssh_client, remote_dir: str) -> list[str]:
        _, out, _ = ssh_client.exec_command(f"find '{remote_dir}' -type f 2>/dev/null")
        lines = out.read().decode().splitlines()
        return [l for l in lines if l]

    def _remote_cleanup(self, ssh_client, remote_dir: str) -> None:
        ssh_client.exec_command(f"rm -rf '{remote_dir}'")

    def test_files_deployed(self, tmp_codebase, ssh_client):
        deploy_path = "mylib/1.2.3"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        args = _make_args(
            target="ssh",
            prefix="mylib",
            ssh_host=SSH_HOST,
            ssh_port=SSH_PORT,
            ssh_user=SSH_USER,
            ssh_password=SSH_PASSWORD,
            ssh_deploy_root=SSH_DEPLOY_ROOT,
            dry_run=False,
        )
        depush.deploy_ssh(args, tmp_codebase, deploy_path)

        remote_files = self._remote_files(ssh_client, remote_root)
        assert any("main.py" in f for f in remote_files)
        assert any("utils.py" in f for f in remote_files)

        self._remote_cleanup(ssh_client, remote_root)

    def test_stale_files_deleted(self, tmp_codebase, ssh_client):
        deploy_path = "mylib/1.2.3"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        # Pre-place a stale file
        ssh_client.exec_command(
            f"mkdir -p '{remote_root}' && echo stale > '{remote_root}/stale.py'"
        )
        import time

        time.sleep(0.2)  # give the remote command time to complete

        args = _make_args(
            target="ssh",
            prefix="mylib",
            ssh_host=SSH_HOST,
            ssh_port=SSH_PORT,
            ssh_user=SSH_USER,
            ssh_password=SSH_PASSWORD,
            ssh_deploy_root=SSH_DEPLOY_ROOT,
            dry_run=False,
        )
        depush.deploy_ssh(args, tmp_codebase, deploy_path)

        remote_files = self._remote_files(ssh_client, remote_root)
        assert not any("stale.py" in f for f in remote_files)

        self._remote_cleanup(ssh_client, remote_root)

    def test_dry_run_deploys_nothing(self, tmp_codebase, ssh_client):
        deploy_path = "mylib/dry-run-test"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        args = _make_args(
            target="ssh",
            prefix="mylib",
            ssh_host=SSH_HOST,
            ssh_port=SSH_PORT,
            ssh_user=SSH_USER,
            ssh_password=SSH_PASSWORD,
            ssh_deploy_root=SSH_DEPLOY_ROOT,
            dry_run=True,
        )
        depush.deploy_ssh(args, tmp_codebase, deploy_path)

        remote_files = self._remote_files(ssh_client, remote_root)
        assert remote_files == []

    def test_idempotent_redeploy(self, tmp_codebase, ssh_client):
        deploy_path = "mylib/1.2.3"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        args = _make_args(
            target="ssh",
            prefix="mylib",
            ssh_host=SSH_HOST,
            ssh_port=SSH_PORT,
            ssh_user=SSH_USER,
            ssh_password=SSH_PASSWORD,
            ssh_deploy_root=SSH_DEPLOY_ROOT,
            dry_run=False,
        )
        depush.deploy_ssh(args, tmp_codebase, deploy_path)
        depush.deploy_ssh(
            args, tmp_codebase, deploy_path
        )  # second call should not error

        remote_files = self._remote_files(ssh_client, remote_root)
        assert any("main.py" in f for f in remote_files)

        self._remote_cleanup(ssh_client, remote_root)

    def test_ignored_file_not_uploaded(self, tmp_codebase, ssh_client):
        deploy_path = "mylib/ignore-upload-test"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)
        (tmp_codebase / ".depushignore").write_text("*.py\n")

        args = _make_args(
            target="ssh",
            prefix="mylib",
            ssh_host=SSH_HOST,
            ssh_port=SSH_PORT,
            ssh_user=SSH_USER,
            ssh_password=SSH_PASSWORD,
            ssh_deploy_root=SSH_DEPLOY_ROOT,
            dry_run=False,
        )
        depush.deploy_ssh(args, tmp_codebase, deploy_path)

        remote_files = self._remote_files(ssh_client, remote_root)
        assert not any(f.endswith(".py") for f in remote_files)

        self._remote_cleanup(ssh_client, remote_root)

    def test_ignored_stale_not_deleted(self, tmp_codebase, ssh_client):
        deploy_path = "mylib/ignore-stale-test"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        # Pre-place a file matching an ignore pattern
        ssh_client.exec_command(
            f"mkdir -p '{remote_root}' && echo KEY=value > '{remote_root}/secrets.env'"
        )
        import time

        time.sleep(0.2)

        (tmp_codebase / ".depushignore").write_text("*.env\n")
        args = _make_args(
            target="ssh",
            prefix="mylib",
            ssh_host=SSH_HOST,
            ssh_port=SSH_PORT,
            ssh_user=SSH_USER,
            ssh_password=SSH_PASSWORD,
            ssh_deploy_root=SSH_DEPLOY_ROOT,
            dry_run=False,
        )
        depush.deploy_ssh(args, tmp_codebase, deploy_path)

        remote_files = self._remote_files(ssh_client, remote_root)
        assert any("secrets.env" in f for f in remote_files)

        self._remote_cleanup(ssh_client, remote_root)


# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDeploySSHRsyncIntegration:
    """Integration tests for the rsync-based SSH deployment path.

    These tests require:
    - ./generate_test_keys.sh has been run (creates test-keys/depush_test_rsa)
    - docker compose build ssh-server (bakes the public key into the image)
    - docker compose up -d (starts the container)
    - rsync installed locally
    """

    @pytest.fixture()
    def _paramiko(self):
        return pytest.importorskip("paramiko")

    @pytest.fixture()
    def rsync_key(self):
        """Return the path to the pre-generated test private key.

        The key lives in test-keys/depush_test_rsa (generated by
        ./generate_test_keys.sh).  The matching public key is baked into the
        ssh-server image at build time, so no dynamic injection is needed.

        Also refreshes the host's known_hosts entry so rsync's underlying ssh
        accepts the container without a host-key-changed error.
        """
        import os
        import shutil
        import subprocess

        if not shutil.which("rsync"):
            pytest.skip("rsync not found in PATH")

        repo_root = Path(__file__).parent.parent
        key_path = repo_root / "test-keys" / "depush_test_rsa"
        if not key_path.exists():
            pytest.skip(
                "test-keys/depush_test_rsa not found — run ./generate_test_keys.sh "
                "then rebuild the ssh-server image"
            )
        key_path.chmod(0o600)

        # Refresh the server's host key entry so rsync's ssh accepts it
        subprocess.run(
            ["ssh-keygen", "-R", f"[{SSH_HOST}]:{SSH_PORT}"],
            capture_output=True,
        )
        keyscan = subprocess.run(
            ["ssh-keyscan", "-p", str(SSH_PORT), SSH_HOST],
            capture_output=True,
            text=True,
        )
        if keyscan.returncode == 0 and keyscan.stdout:
            ssh_dir = os.path.expanduser("~/.ssh")
            os.makedirs(ssh_dir, exist_ok=True)
            with open(os.path.join(ssh_dir, "known_hosts"), "a") as f:
                f.write(keyscan.stdout)

        yield key_path

    @pytest.fixture()
    def ssh_client(self, _paramiko, rsync_key):
        """Return an open paramiko SSHClient using key-based auth (same key rsync will use)."""
        client = _paramiko.SSHClient()
        client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=SSH_HOST,
                port=SSH_PORT,
                username=SSH_USER,
                key_filename=str(rsync_key),
            )
        except Exception as exc:
            pytest.skip(f"Key-based SSH failed: {exc}")
        yield client
        client.close()

    def _remote_files(self, ssh_client, remote_dir: str) -> list[str]:
        _, out, _ = ssh_client.exec_command(f"find '{remote_dir}' -type f 2>/dev/null")
        lines = out.read().decode().splitlines()
        return [l for l in lines if l]

    def _remote_cleanup(self, ssh_client, remote_dir: str) -> None:
        ssh_client.exec_command(f"rm -rf '{remote_dir}'")
        import time

        time.sleep(0.1)

    def _make_rsync_args(self, rsync_key, **kwargs):
        defaults = dict(
            target="ssh",
            prefix="mylib",
            ssh_host=SSH_HOST,
            ssh_port=SSH_PORT,
            ssh_user=SSH_USER,
            ssh_password=None,
            ssh_key_file=str(rsync_key),
            ssh_deploy_root=SSH_DEPLOY_ROOT,
            dry_run=False,
        )
        defaults.update(kwargs)
        return _make_args(**defaults)

    def test_rsync_files_deployed(self, tmp_codebase, rsync_key, ssh_client):
        deploy_path = "mylib/rsync-1.0.0"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        args = self._make_rsync_args(rsync_key)
        depush.deploy_ssh(args, tmp_codebase, deploy_path)

        remote_files = self._remote_files(ssh_client, remote_root)
        assert any("main.py" in f for f in remote_files)
        assert any("utils.py" in f for f in remote_files)

        self._remote_cleanup(ssh_client, remote_root)

    def test_rsync_stale_files_deleted(self, tmp_codebase, rsync_key, ssh_client):
        deploy_path = "mylib/rsync-stale-test"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        # Pre-place a stale file on remote
        ssh_client.exec_command(
            f"mkdir -p '{remote_root}' && echo stale > '{remote_root}/stale.py'"
        )
        import time

        time.sleep(0.3)

        args = self._make_rsync_args(rsync_key)
        depush.deploy_ssh(args, tmp_codebase, deploy_path)

        remote_files = self._remote_files(ssh_client, remote_root)
        assert not any(
            "stale.py" in f for f in remote_files
        ), "rsync --delete should have removed stale.py"

        self._remote_cleanup(ssh_client, remote_root)

    def test_rsync_idempotent_redeploy(self, tmp_codebase, rsync_key, ssh_client):
        deploy_path = "mylib/rsync-idempotent"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        args = self._make_rsync_args(rsync_key)
        depush.deploy_ssh(args, tmp_codebase, deploy_path)
        depush.deploy_ssh(
            args, tmp_codebase, deploy_path
        )  # second call should not error

        remote_files = self._remote_files(ssh_client, remote_root)
        assert any("main.py" in f for f in remote_files)

        self._remote_cleanup(ssh_client, remote_root)

    def test_rsync_ignored_file_not_uploaded(self, tmp_codebase, rsync_key, ssh_client):
        deploy_path = "mylib/rsync-ignore-test"
        remote_root = f"{SSH_DEPLOY_ROOT}/{deploy_path}"
        self._remote_cleanup(ssh_client, remote_root)

        (tmp_codebase / ".depushignore").write_text("*.py\n")
        args = self._make_rsync_args(rsync_key)
        depush.deploy_ssh(args, tmp_codebase, deploy_path)

        remote_files = self._remote_files(ssh_client, remote_root)
        assert not any(
            f.endswith(".py") for f in remote_files
        ), ".py files should be excluded by .depushignore"

        self._remote_cleanup(ssh_client, remote_root)
