#!/usr/bin/env python3
"""
depush — deploy a versioned codebase directory to S3/MinIO, SSH, or a local directory.

All options can be set via CLI flags, environment variables, or a YAML config file.
Precedence (highest to lowest): CLI flag > environment variable > YAML config > default.

Usage (local):
  depush --target s3 --prefix mylib
  depush --target ssh --prefix mylib
  depush --target local --prefix mylib --local-dest /srv/releases
  depush --config deploy-config.yaml

Usage (Docker):
  docker run --rm -v ./codebase:/codebase \\
    -e DEPLOY_TARGET=s3 -e DEPLOY_PREFIX=mylib \\
    -e S3_BUCKET=deployments -e S3_ENDPOINT=http://minio:9000 \\
    -e S3_ACCESS_KEY=admin -e S3_SECRET_KEY=admin \\
    tfdeploy-deploy

Environment variables:
  General:
    DEPLOY_TARGET          Deployment target: s3, ssh, or local (required)
    DEPLOY_PREFIX          Path prefix, e.g. 'mylib' -> 'mylib/{version}/' (required)
    DEPLOY_CODEBASE_DIR    Path to codebase directory containing a 'version' file (default: .)
    DEPLOY_DRY_RUN         Set to '1', 'true', or 'yes' to preview without uploading
    DEPLOY_CONFIG          Path to a YAML config file (default: depush.yaml in CWD if present)

  Local directory:
    DEPLOY_LOCAL_DEST      Root destination directory for local deployments (default: ./dist)

  S3 / MinIO:
    S3_BUCKET              Bucket name (required for s3 target)
    S3_ENDPOINT            Custom endpoint URL for MinIO, e.g. http://localhost:9000
    S3_REGION              AWS region (default: us-east-1)
    S3_ACCESS_KEY          Access key / MinIO username
    S3_SECRET_KEY          Secret key / MinIO password

  SSH:
    SSH_HOST               SSH server hostname or IP (required for ssh target)
    SSH_PORT               SSH port (default: 22)
    SSH_USER               SSH username (default: admin)
    SSH_PASSWORD           SSH password (leave unset to use key-based auth)
    SSH_KEY_FILE           Path to private key file
    SSH_DEPLOY_ROOT        Absolute path on the remote server (default: /deployments)
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Config resolution constants
# ---------------------------------------------------------------------------

# Hardcoded fallback defaults
DEFAULTS: dict = {
    "codebase_dir": ".",
    "dry_run": False,
    "local_dest": "./dist",
    "s3_region": "us-east-1",
    "ssh_port": 22,
    "ssh_user": "admin",
    "ssh_deploy_root": "/deployments",
}

# Mapping from argparse dest -> environment variable name
ENV_MAP: dict[str, str] = {
    "target": "DEPLOY_TARGET",
    "prefix": "DEPLOY_PREFIX",
    "codebase_dir": "DEPLOY_CODEBASE_DIR",
    "dry_run": "DEPLOY_DRY_RUN",
    "local_dest": "DEPLOY_LOCAL_DEST",
    "s3_bucket": "S3_BUCKET",
    "s3_endpoint": "S3_ENDPOINT",
    "s3_region": "S3_REGION",
    "s3_profile": "S3_PROFILE",
    "s3_access_key": "S3_ACCESS_KEY",
    "s3_secret_key": "S3_SECRET_KEY",
    "ssh_host": "SSH_HOST",
    "ssh_port": "SSH_PORT",
    "ssh_user": "SSH_USER",
    "ssh_password": "SSH_PASSWORD",
    "ssh_key_file": "SSH_KEY_FILE",
    "ssh_deploy_root": "SSH_DEPLOY_ROOT",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_yaml_config(path: str) -> dict:
    """Load and flatten a YAML config file into a flat dict matching argparse dests."""
    try:
        import yaml
    except ImportError:
        sys.exit("Error: PyYAML is not installed. Run: pip install pyyaml")

    config_path = Path(path)
    if not config_path.exists():
        sys.exit(f"Error: config file not found: {config_path}")

    with config_path.open() as f:
        raw = yaml.safe_load(f) or {}

    flat: dict = {}

    # Top-level general keys
    for key in ("target", "prefix", "codebase_dir", "dry_run"):
        if key in raw:
            flat[key] = raw[key]

    # Nested sections -> flattened with section prefix
    section_map = {
        "local": {"dest": "local_dest"},
        "s3": {
            "bucket": "s3_bucket",
            "endpoint": "s3_endpoint",
            "region": "s3_region",
            "profile": "s3_profile",
            "access_key": "s3_access_key",
            "secret_key": "s3_secret_key",
        },
        "ssh": {
            "host": "ssh_host",
            "port": "ssh_port",
            "user": "ssh_user",
            "password": "ssh_password",
            "key_file": "ssh_key_file",
            "deploy_root": "ssh_deploy_root",
        },
    }
    for section, mapping in section_map.items():
        if section in raw and isinstance(raw[section], dict):
            for yaml_key, dest_key in mapping.items():
                if yaml_key in raw[section]:
                    flat[dest_key] = raw[section][yaml_key]

    return flat


def resolve_defaults(yaml_cfg: dict) -> dict:
    """
    Build a merged defaults dict with precedence:
    env var > YAML config > hardcoded default.
    Returns a dict suitable for parser.set_defaults().
    """
    merged = dict(DEFAULTS)

    # Layer in YAML values
    for key, value in yaml_cfg.items():
        merged[key] = value

    # Layer in environment variables (highest priority before CLI)
    for dest, env_var in ENV_MAP.items():
        raw = os.environ.get(env_var)
        if raw is None:
            continue
        # Type coercion for known numeric/bool fields
        if dest == "ssh_port":
            try:
                merged[dest] = int(raw)
            except ValueError:
                sys.exit(f"Error: {env_var} must be an integer, got: {raw!r}")
        elif dest == "dry_run":
            merged[dest] = raw.lower() in ("1", "true", "yes")
        else:
            merged[dest] = raw

    return merged


def read_version(codebase_dir: Path) -> str:
    version_file = codebase_dir / "version"
    if not version_file.exists():
        sys.exit(f"Error: version file not found at {version_file}")
    return version_file.read_text().strip()


def load_ignore_spec(codebase_dir: Path):
    """Load .depushignore from codebase_dir and return a pathspec matcher, or None."""
    ignore_file = codebase_dir / ".depushignore"
    if not ignore_file.exists():
        return None
    try:
        import pathspec
    except ImportError:
        sys.exit("Error: pathspec is not installed. Run: pip install pathspec")
    lines = ignore_file.read_text().splitlines()
    return pathspec.PathSpec.from_lines("gitignore", lines)


def collect_files(codebase_dir: Path, ignore_spec=None) -> list[Path]:
    """Return all files under codebase_dir, excluding .git, depush.yaml, .depushignore, and any .depushignore patterns."""
    excluded = {codebase_dir / "depush.yaml", codebase_dir / ".depushignore"}
    files = []
    for p in codebase_dir.rglob("*"):
        if not p.is_file():
            continue
        if ".git" in p.parts:
            continue
        if p in excluded:
            continue
        if ignore_spec and ignore_spec.match_file(str(p.relative_to(codebase_dir))):
            continue
        files.append(p)
    return sorted(files)


# ---------------------------------------------------------------------------
# Local directory deployment
# ---------------------------------------------------------------------------


def deploy_local(
    args: argparse.Namespace, codebase_dir: Path, deploy_path: str
) -> None:
    dest = Path(args.local_dest) / deploy_path
    ignore_spec = load_ignore_spec(codebase_dir)

    files = collect_files(codebase_dir, ignore_spec)
    if not files:
        sys.exit("Error: no files found in codebase directory")

    for file_path in files:
        relative = file_path.relative_to(codebase_dir)
        target = dest / relative
        if args.dry_run:
            print(f"  [dry-run] {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, target)
            print(f"  {target}")

    # Delete files at destination that no longer exist in codebase
    codebase_relatives = {f.relative_to(codebase_dir) for f in files}
    deleted = 0
    if dest.exists():
        for existing in sorted(dest.rglob("*")):
            if not existing.is_file():
                continue
            rel = existing.relative_to(dest)
            if rel in codebase_relatives:
                continue
            if ignore_spec and ignore_spec.match_file(str(rel)):
                continue
            deleted += 1
            if args.dry_run:
                print(f"  [dry-run] delete {existing}")
            else:
                existing.unlink()
                print(f"  deleted {existing}")

    tag = "[dry-run] " if args.dry_run else ""
    suffix = f", {deleted} deleted" if deleted else ""
    print(f"\n{tag}Deployed {len(files)} file(s) to {dest}/{suffix}")


# ---------------------------------------------------------------------------
# S3 / MinIO deployment
# ---------------------------------------------------------------------------


def deploy_s3(args: argparse.Namespace, codebase_dir: Path, deploy_path: str) -> None:
    try:
        import boto3
        from botocore.client import Config
    except ImportError:
        sys.exit("Error: boto3 is not installed. Run: pip install boto3")

    ignore_spec = load_ignore_spec(codebase_dir)

    session = boto3.Session(
        profile_name=args.s3_profile or None,
        aws_access_key_id=args.s3_access_key or None,
        aws_secret_access_key=args.s3_secret_key or None,
        region_name=args.s3_region,
    )
    client_kwargs: dict = {}
    if args.s3_endpoint:
        client_kwargs["endpoint_url"] = args.s3_endpoint
        client_kwargs["config"] = Config(signature_version="s3v4")

    s3 = session.client("s3", **client_kwargs)

    files = collect_files(codebase_dir, ignore_spec)
    if not files:
        sys.exit("Error: no files found in codebase directory")

    for file_path in files:
        relative = file_path.relative_to(codebase_dir)
        key = f"{deploy_path}/{relative}"
        if args.dry_run:
            print(f"  [dry-run] s3://{args.s3_bucket}/{key}")
        else:
            print(f"  s3://{args.s3_bucket}/{key}")
            s3.upload_file(str(file_path), args.s3_bucket, key)

    # Delete objects that no longer exist in codebase
    expected_keys = {f"{deploy_path}/{f.relative_to(codebase_dir)}" for f in files}
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=args.s3_bucket, Prefix=f"{deploy_path}/"):
        for obj in page.get("Contents", []):
            if obj["Key"] in expected_keys:
                continue
            rel_key = obj["Key"][len(deploy_path) + 1 :]
            if ignore_spec and ignore_spec.match_file(rel_key):
                continue
            deleted += 1
            if args.dry_run:
                print(f"  [dry-run] delete s3://{args.s3_bucket}/{obj['Key']}")
            else:
                s3.delete_object(Bucket=args.s3_bucket, Key=obj["Key"])
                print(f"  deleted s3://{args.s3_bucket}/{obj['Key']}")

    tag = "[dry-run] " if args.dry_run else ""
    suffix = f", {deleted} deleted" if deleted else ""
    print(
        f"\n{tag}Deployed {len(files)} file(s) to s3://{args.s3_bucket}/{deploy_path}/{suffix}"
    )


# ---------------------------------------------------------------------------
# SSH / remote filesystem deployment
# ---------------------------------------------------------------------------


def deploy_ssh(args: argparse.Namespace, codebase_dir: Path, deploy_path: str) -> None:
    try:
        import paramiko
    except ImportError:
        sys.exit("Error: paramiko is not installed. Run: pip install paramiko")

    ignore_spec = load_ignore_spec(codebase_dir)
    remote_root = f"{args.ssh_deploy_root}/{deploy_path}"

    if args.dry_run:
        files = collect_files(codebase_dir, ignore_spec)
        for f in files:
            rel = f.relative_to(codebase_dir)
            print(f"  [dry-run] {args.ssh_user}@{args.ssh_host}:{remote_root}/{rel}")
        print(f"\n[dry-run] Would deploy {len(files)} file(s) to {remote_root}/")
        print(
            "[dry-run] Stale remote files would also be deleted (requires connection to determine)"
        )
        return

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {
        "hostname": args.ssh_host,
        "port": args.ssh_port,
        "username": args.ssh_user,
    }
    if args.ssh_password:
        connect_kwargs["password"] = args.ssh_password
    elif args.ssh_key_file:
        connect_kwargs["key_filename"] = args.ssh_key_file

    try:
        client.connect(**connect_kwargs)
    except Exception as e:
        sys.exit(f"Error: SSH connection failed: {e}")

    def remote_exec(cmd: str) -> None:
        _, stdout, stderr = client.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            sys.exit(
                f"Error: remote command failed ({exit_code}): {cmd}\n{stderr.read().decode()}"
            )

    sftp = client.open_sftp()

    remote_exec(f"mkdir -p '{remote_root}'")

    files = collect_files(codebase_dir, ignore_spec)
    for file_path in files:
        relative = file_path.relative_to(codebase_dir)
        remote_file = f"{remote_root}/{relative}"
        remote_dir = str(Path(remote_file).parent)
        remote_exec(f"mkdir -p '{remote_dir}'")
        print(f"  {args.ssh_user}@{args.ssh_host}:{remote_file}")
        sftp.put(str(file_path), remote_file)

    # Delete remote files that no longer exist in codebase
    codebase_relatives = {str(f.relative_to(codebase_dir)) for f in files}
    expected_remotes = {f"{remote_root}/{rel}" for rel in codebase_relatives}
    _, ls_out, _ = client.exec_command(f"find '{remote_root}' -type f")
    remote_files = set(ls_out.read().decode().splitlines())
    deleted = 0
    for stale in sorted(remote_files - expected_remotes):
        rel_path = stale[len(remote_root) + 1 :]
        if ignore_spec and ignore_spec.match_file(rel_path):
            continue
        deleted += 1
        remote_exec(f"rm -f '{stale}'")
        print(f"  deleted {args.ssh_user}@{args.ssh_host}:{stale}")

    sftp.close()
    client.close()
    suffix = f", {deleted} deleted" if deleted else ""
    print(
        f"\nDeployed {len(files)} file(s) to {args.ssh_user}@{args.ssh_host}:{remote_root}/{suffix}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy a versioned codebase directory to S3/MinIO, SSH, or a local directory.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--config",
        default=os.environ.get("DEPLOY_CONFIG"),
        metavar="FILE",
        help="Path to a YAML config file  [env: DEPLOY_CONFIG]",
    )
    parser.add_argument(
        "--target",
        choices=["s3", "ssh", "local"],
        help="Deployment target (required)  [env: DEPLOY_TARGET]",
    )
    parser.add_argument(
        "--prefix",
        help="Path prefix, e.g. 'mylib' -> 'mylib/{version}/'  [env: DEPLOY_PREFIX]",
    )
    parser.add_argument(
        "--codebase-dir",
        dest="codebase_dir",
        help="Path to codebase directory containing a 'version' file (default: .)  [env: DEPLOY_CODEBASE_DIR]",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Print what would be deployed without uploading  [env: DEPLOY_DRY_RUN]",
    )

    # Local directory options
    local_group = parser.add_argument_group("Local directory options")
    local_group.add_argument(
        "--local-dest",
        dest="local_dest",
        help="Root destination directory  [env: DEPLOY_LOCAL_DEST]",
    )

    # S3 / MinIO options
    s3_group = parser.add_argument_group("S3 / MinIO options")
    s3_group.add_argument("--s3-bucket", dest="s3_bucket", help="[env: S3_BUCKET]")
    s3_group.add_argument(
        "--s3-endpoint",
        dest="s3_endpoint",
        help="Custom endpoint URL for MinIO  [env: S3_ENDPOINT]",
    )
    s3_group.add_argument("--s3-region", dest="s3_region", help="[env: S3_REGION]")
    s3_group.add_argument(
        "--s3-profile",
        dest="s3_profile",
        help="AWS credentials profile name  [env: S3_PROFILE]",
    )
    s3_group.add_argument(
        "--s3-access-key", dest="s3_access_key", help="[env: S3_ACCESS_KEY]"
    )
    s3_group.add_argument(
        "--s3-secret-key", dest="s3_secret_key", help="[env: S3_SECRET_KEY]"
    )

    # SSH options
    ssh_group = parser.add_argument_group("SSH options")
    ssh_group.add_argument("--ssh-host", dest="ssh_host", help="[env: SSH_HOST]")
    ssh_group.add_argument(
        "--ssh-port", dest="ssh_port", type=int, help="[env: SSH_PORT]"
    )
    ssh_group.add_argument("--ssh-user", dest="ssh_user", help="[env: SSH_USER]")
    ssh_group.add_argument(
        "--ssh-password", dest="ssh_password", help="[env: SSH_PASSWORD]"
    )
    ssh_group.add_argument(
        "--ssh-key-file",
        dest="ssh_key_file",
        help="Path to private key  [env: SSH_KEY_FILE]",
    )
    ssh_group.add_argument(
        "--ssh-deploy-root", dest="ssh_deploy_root", help="[env: SSH_DEPLOY_ROOT]"
    )

    return parser


def validate(args: argparse.Namespace) -> None:
    if not args.target:
        sys.exit(
            "Error: --target (or DEPLOY_TARGET) is required. Choose from: s3, ssh, local"
        )
    if not args.prefix:
        sys.exit("Error: --prefix (or DEPLOY_PREFIX) is required")
    if args.target == "s3" and not args.s3_bucket:
        sys.exit("Error: --s3-bucket (or S3_BUCKET) is required for s3 target")
    if args.target == "ssh" and not args.ssh_host:
        sys.exit("Error: --ssh-host (or SSH_HOST) is required for ssh target")
    if args.target == "local" and not args.local_dest:
        sys.exit(
            "Error: --local-dest (or DEPLOY_LOCAL_DEST) is required for local target"
        )


def main() -> None:
    parser = build_parser()

    # Pre-parse to get --config / DEPLOY_CONFIG before setting defaults
    pre, _ = parser.parse_known_args()
    config_path = pre.config or (
        "depush.yaml" if Path("depush.yaml").exists() else None
    )
    yaml_cfg = load_yaml_config(config_path) if config_path else {}

    # Merge: env vars > YAML > hardcoded defaults, then inject as argparse defaults
    merged_defaults = resolve_defaults(yaml_cfg)
    parser.set_defaults(**merged_defaults)

    args = parser.parse_args()
    validate(args)

    codebase_dir = Path(args.codebase_dir).resolve()
    if not codebase_dir.is_dir():
        sys.exit(f"Error: codebase directory not found: {codebase_dir}")

    version = read_version(codebase_dir)
    deploy_path = f"{args.prefix}/{version}"

    print(f"Version  : {version}")
    print(f"Path     : {deploy_path}/")
    print(f"Target   : {args.target}")
    effective_config = args.config or (
        "depush.yaml" if Path("depush.yaml").exists() else None
    )
    if effective_config:
        print(f"Config   : {effective_config}")
    if args.dry_run:
        print("Mode     : dry-run")
    print()

    if args.target == "s3":
        deploy_s3(args, codebase_dir, deploy_path)
    elif args.target == "ssh":
        deploy_ssh(args, codebase_dir, deploy_path)
    else:
        deploy_local(args, codebase_dir, deploy_path)


if __name__ == "__main__":
    main()
