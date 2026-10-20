#!/usr/bin/env python3
"""Upload encrypted Touming backup artifacts to Tencent COS.

The backup shell scripts decide what must be backed up and encrypted. This
helper only uploads the already-produced artifact/checksum pair so server COS
credentials can stay in root-only environment files instead of shell history.
"""

import os
import sys
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required env {name}")
    return value


def upload(client: CosS3Client, bucket: str, prefix: str, path: str) -> None:
    file_path = Path(path)
    if not file_path.is_file():
        raise SystemExit(f"not a file: {path}")

    key = f"{prefix}/{file_path.name}" if prefix else file_path.name
    client.put_object_from_local_file(
        Bucket=bucket,
        LocalFilePath=str(file_path),
        Key=key,
    )
    print(f"uploaded {file_path.name} -> cos://{bucket}/{key}")


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: cos_upload.py ARTIFACT [CHECKSUM]")

    secret_id = require("COS_SECRET_ID")
    secret_key = require("COS_SECRET_KEY")
    region = require("COS_REGION")
    bucket = require("COS_BUCKET")
    token = os.environ.get("COS_TOKEN") or None
    scheme = os.environ.get("COS_SCHEME", "https") or "https"
    prefix = os.environ.get("COS_BACKUP_PREFIX", "backups").strip().strip("/")

    config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=token,
        Scheme=scheme,
    )
    client = CosS3Client(config)
    for item in sys.argv[1:]:
        upload(client, bucket, prefix, item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
