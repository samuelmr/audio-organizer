"""Object storage backends. The sync engine only needs these few operations, so other
backends (or a hosted service's own storage) can be added beside S3."""

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import boto3


class ObjectStore(Protocol):
    def upload(self, key: str, path: Path, content_type: str, metadata: dict[str, str]) -> None: ...

    def copy(self, source: str, key: str, content_type: str, metadata: dict[str, str]) -> None:
        """Server-side copy; with source == key, replaces the object's metadata in place."""

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str) -> Iterator[tuple[str, int]]:
        """(key, size) of every object under the prefix."""


class S3Store:
    """Any S3-compatible storage. Credentials come from the environment, ~/.aws or the
    named profile."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        profile: str | None = None,
    ) -> None:
        session = boto3.Session(profile_name=profile, region_name=region)
        self.client = session.client("s3", endpoint_url=endpoint_url)
        self.bucket = bucket

    def upload(self, key: str, path: Path, content_type: str, metadata: dict[str, str]) -> None:
        self.client.upload_file(
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "Metadata": metadata},
        )

    def copy(self, source: str, key: str, content_type: str, metadata: dict[str, str]) -> None:
        self.client.copy_object(
            Bucket=self.bucket,
            Key=key,
            CopySource={"Bucket": self.bucket, "Key": source},
            MetadataDirective="REPLACE",
            ContentType=content_type,
            Metadata=metadata,
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def list(self, prefix: str) -> Iterator[tuple[str, int]]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"], item["Size"]
