from types import SimpleNamespace as Item

import pytest

from engrai_server.model_browser import ModelBrowser, gguf_variants, repository_id


def file(name, size=100):
    return Item(rfilename=name, size=size)


def test_variants_group_shards_without_merging_distinct_models():
    variants = gguf_variants([
        file("a-Q4_K_M-00002-of-00002.gguf"),
        file("a-Q4_K_M-00001-of-00002.gguf"),
        file("b-Q4_K_M.gguf"), file("a-Q8_0.gguf", 300),
        file("mmproj-F16.gguf"), file("config.json"), file("weights.safetensors"),
    ])
    assert len(variants) == 3
    assert variants[0]["quantization"] == "Q4_K_M"
    assert variants[0]["bytes_total"] == 200
    assert variants[0]["complete"]
    assert variants[0]["files"] == ["a-Q4_K_M-00001-of-00002.gguf", "a-Q4_K_M-00002-of-00002.gguf"]
    assert variants[2]["id"] == "b-Q4_K_M.gguf"


def test_incomplete_and_inconsistent_shards_are_not_selectable():
    for names in [
        ["a-Q4-00001-of-00002.gguf"],
        ["a-Q4-00001-of-00002.gguf", "a-Q4-00002-of-00003.gguf"],
        ["a-Q4-00000-of-00002.gguf", "a-Q4-00001-of-00002.gguf"],
        ["a-Q4.gguf", "a-Q4-00001-of-00001.gguf"],
    ]:
        assert not gguf_variants([file(name) for name in names])[0]["complete"]


@pytest.mark.parametrize("name,quant", [
    ("model-UD-IQ2_XXS.gguf", "UD-IQ2_XXS"),
    ("model-BF16.GGUF", "BF16"), ("Q4_K_M/model.gguf", "Q4_K_M"),
    ("model-fp16.gguf", "FP16"),
    ("model-MXFP4.gguf", "MXFP4"), ("model.gguf", "Unspecified"),
])
def test_quant_labels_and_unknown_sizes(name, quant):
    variant = gguf_variants([file(name, None)])[0]
    assert variant["quantization"] == quant
    assert variant["bytes_total"] is None


def test_hub_search_is_filtered_and_bounded_and_variants_pin_commit():
    class Hub:
        def list_models(self, **kwargs):
            assert kwargs == {"search": "llama", "filter": "gguf", "sort": "downloads", "limit": 20}
            return [Item(id="owner/model", downloads=123)]

        def model_info(self, repo, **kwargs):
            assert repo == "owner/model"
            assert kwargs == {"files_metadata": True, "timeout": 15}
            return Item(sha="a" * 40, siblings=[file("model-Q4_K_M.gguf")])

    browser = ModelBrowser(Hub())
    assert browser.search("llama")["models"] == [{"repo_id": "owner/model", "downloads": 123}]
    assert browser.variants("owner/model")["commit_hash"] == "a" * 40


def test_direct_model_page_search_and_empty_repository():
    class Hub:
        def model_info(self, repo, **kwargs):
            assert repo == "owner/model"
            return Item(id=repo, sha="a" * 40, siblings=[file("README.md")])
    browser = ModelBrowser(Hub())
    assert browser.search("https://huggingface.co/owner/model")["models"] == []
    with pytest.raises(ValueError, match="No standalone GGUF"):
        browser.variants("owner/model")


@pytest.mark.parametrize("value", ["https://evil.test/owner/model", "https://huggingface.co.evil.test/a/b", "../escape", "owner/model/other"])
def test_repository_validation(value):
    with pytest.raises(ValueError):
        repository_id(value)


def test_repository_page_normalization():
    assert repository_id("https://huggingface.co/owner/model/tree/main") == "owner/model"
