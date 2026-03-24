# depush

Deploy a versioned codebase directory to **S3/MinIO**, **SSH**, or a **local directory**.

depush reads a `version` file from your codebase directory and deploys all files to `{prefix}/{version}/` at the destination. Stale files at the destination that no longer exist in the source are automatically deleted, keeping deployments in sync.

Configuration is flexible: use CLI flags, environment variables, or a YAML config file. Precedence (highest to lowest): **CLI flag > environment variable > YAML config > default**.

---

## Installation

```bash
pip install depush
```

Or with [uv](https://github.com/astral-sh/uv):

```bash
uv add depush
```

---

## Quick start

Your codebase directory must contain a `version` file:

```
my-project/
├── version        # contains e.g. "1.2.3"
├── main.py
└── ...
```

### Deploy to S3 / MinIO

```bash
depush --target s3 --prefix mylib \
  --s3-bucket deployments \
  --s3-endpoint http://localhost:9000 \
  --s3-access-key admin \
  --s3-secret-key secret
```

Files are uploaded to `s3://deployments/mylib/1.2.3/`.

### Deploy over SSH

```bash
depush --target ssh --prefix mylib \
  --ssh-host myserver.example.com \
  --ssh-user deploy \
  --ssh-key-file ~/.ssh/id_rsa
```

Files are copied to `/deployments/mylib/1.2.3/` on the remote host.

### Deploy to a local directory

```bash
depush --target local --prefix mylib --local-dest /srv/releases
```

Files are copied to `/srv/releases/mylib/1.2.3/`.

### Dry run

Add `--dry-run` to any command to preview what would be deployed without making any changes:

```bash
depush --target s3 --prefix mylib --s3-bucket deployments --dry-run
```

---

## Configuration file

Place a `depush.yaml` in the current directory (or pass `--config path/to/file.yaml`) to avoid repeating flags:

```yaml
target: s3
prefix: mylib
codebase_dir: ./codebase
dry_run: false

s3:
  bucket: deployments
  endpoint: http://localhost:9000   # omit for AWS
  region: us-east-1
  access_key: admin
  secret_key: secret

# local:
#   dest: ./dist

# ssh:
#   host: myserver.example.com
#   port: 22
#   user: deploy
#   key_file: ~/.ssh/id_rsa
#   deploy_root: /deployments
```

---

## Environment variables

All options can also be set via environment variables:

| Variable | Description | Default |
|---|---|---|
| `DEPUSH_DEPLOY_TARGET` | Deployment target: `s3`, `ssh`, or `local` | *(required)* |
| `DEPUSH_DEPLOY_PREFIX` | Path prefix, e.g. `mylib` → `mylib/{version}/` | *(required)* |
| `DEPUSH_DEPLOY_CODEBASE_DIR` | Path to codebase directory | `.` |
| `DEPUSH_DEPLOY_DRY_RUN` | Set to `1`, `true`, or `yes` to preview | `false` |
| `DEPUSH_DEPLOY_CONFIG` | Path to a YAML config file | `depush.yaml` if present |
| `DEPUSH_DEPLOY_LOCAL_DEST` | Root destination for local deployments | `./dist` |
| `DEPUSH_S3_BUCKET` | S3/MinIO bucket name | *(required for s3)* |
| `DEPUSH_S3_ENDPOINT` | Custom endpoint URL for MinIO | *(AWS default)* |
| `DEPUSH_S3_REGION` | AWS region; falls back to `AWS_DEFAULT_REGION` | `us-east-1` |
| `DEPUSH_S3_PROFILE` | AWS credentials profile name (from `~/.aws/credentials`); falls back to `AWS_PROFILE` | |
| `DEPUSH_S3_ACCESS_KEY` | Access key / MinIO username; falls back to `AWS_ACCESS_KEY_ID` | |
| `DEPUSH_S3_SECRET_KEY` | Secret key / MinIO password; falls back to `AWS_SECRET_ACCESS_KEY` | |
| `DEPUSH_SSH_HOST` | SSH server hostname or IP | *(required for ssh)* |
| `DEPUSH_SSH_PORT` | SSH port | `22` |
| `DEPUSH_SSH_USER` | SSH username | `admin` |
| `DEPUSH_SSH_PASSWORD` | SSH password (omit to use key auth) | |
| `DEPUSH_SSH_KEY_FILE` | Path to private key file | |
| `DEPUSH_SSH_DEPLOY_ROOT` | Absolute path on the remote server | `/deployments` |

---

## Ignoring files

Create a `.depushignore` file in your codebase directory using the same syntax as `.gitignore` to exclude files from deployment:

```
*.log
__pycache__/
.env
```

---

## Programmatic use

```python
from depush import depush

# Parse args and deploy
depush.main()

# Or use individual functions
files = depush.collect_files(Path("./my-codebase"))
```
