"""Read-only Hub discovery. File variants are not runtime compatibility claims."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from huggingface_hub import HfApi
from huggingface_hub.utils import validate_repo_id

from .model_downloads import DownloadError, ModelPullRequest


SHARD = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$", re.I)
QUANT = re.compile(
    r"(?<![A-Z0-9])((?:UD-)?(?:IQ|TQ|Q)\d+(?:_[A-Z0-9]+)*|BF16|FP?16|FP?32|MXFP4)(?![A-Z0-9])",
    re.I,
)


def repository_id(value: str) -> str:
    value = value.strip()
    if "://" in value:
        url = urlsplit(value)
        if url.scheme != "https" or url.netloc not in {"huggingface.co", "www.huggingface.co", "hf.co"}:
            raise DownloadError("Paste a Hugging Face model page or owner/name.")
        value = "/".join(url.path.strip("/").split("/")[:2])
    validate_repo_id(value)
    if "/" not in value:
        raise DownloadError("Choose a model repository written as owner/name.")
    return value


def gguf_variants(siblings: list[Any]) -> list[dict[str, Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for file in siblings:
        name = file.rfilename
        if not name.lower().endswith(".gguf"):
            continue
        # Keep multimodal projectors out of the model/quant list. They are not
        # standalone models and must not be mistaken for another F16 variant.
        if "mmproj" in name.lower():
            continue
        ModelPullRequest.valid_filenames([name])
        match = SHARD.match(name)
        key = match[1] + ".gguf" if match else name
        groups[key].append(file)
    variants = []
    for key, files in sorted(groups.items()):
        files.sort(key=lambda file: file.rfilename)
        shards = [SHARD.match(file.rfilename) for file in files]
        complete = True
        if any(shards):
            counts = {int(match[3]) for match in shards if match}
            indices = {int(match[2]) for match in shards if match}
            complete = (
                all(shards) and len(counts) == 1 and len(indices) == len(files)
                and len(files) == next(iter(counts))
                and min(indices) == 1 and max(indices) == len(files)
            )
        quant = QUANT.search(PurePosixPath(key).stem) or QUANT.search(key)
        sizes = [getattr(file, "size", None) for file in files]
        variants.append({
            "id": key,
            "quantization": quant[1].upper() if quant else "Unspecified",
            "files": [file.rfilename for file in files],
            "bytes_total": sum(sizes) if all(size is not None for size in sizes) else None,
            "complete": complete,
        })
    return variants


class ModelBrowser:
    def __init__(self, api: Any = None):
        self.api = api if api is not None else HfApi()

    def search(self, query: str) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise DownloadError("Enter a model name or Hugging Face model page.")
        if "/" in query:
            info = self.api.model_info(repository_id(query), timeout=15)
            models = [info] if any(f.rfilename.lower().endswith(".gguf") for f in info.siblings or []) else []
        else:
            models = self.api.list_models(search=query, filter="gguf", sort="downloads", limit=20)
        return {"models": [
            {"repo_id": model.id, "downloads": getattr(model, "downloads", 0) or 0}
            for model in models
        ]}

    def variants(self, repo: str) -> dict[str, Any]:
        repo = repository_id(repo)
        info = self.api.model_info(repo, files_metadata=True, timeout=15)
        if not info.sha:
            raise DownloadError("The repository did not return an immutable revision.")
        variants = gguf_variants(info.siblings or [])
        if not variants:
            raise DownloadError("No standalone GGUF model files found in this repository.")
        return {"repo_id": repo, "commit_hash": info.sha, "variants": variants}
