# Third-party software

ENGRAI SERVER builds its runtimes from pinned upstream sources. The exact tag,
commit, build options, and payload checksums are recorded in each runtime
bundle, and the upstream license texts are distributed inside every bundle
under `licenses/`.

| Runtime | Upstream | License |
| --- | --- | --- |
| `engrai-text` | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT |
| `engrai-image` | [leejet/stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) | MIT |

Both runtimes also compile in libraries their upstreams vendor. Every one of
their license texts ships in the bundle's `licenses/` directory:

| Library | License | Runtime |
| --- | --- | --- |
| [ggml](https://github.com/ggml-org/ggml) (stable-diffusion.cpp uses [a patched fork](https://github.com/leejet/ggml)) | MIT | text, image |
| [cpp-httplib](https://github.com/yhirose/cpp-httplib) | MIT | text, image |
| [JSON for Modern C++](https://github.com/nlohmann/json) | MIT | text, image |
| [stb](https://github.com/nothings/stb) | MIT or public domain | text, image |
| [miniaudio](https://github.com/mackron/miniaudio) | MIT-0 or public domain | text |
| [subprocess.h](https://github.com/sheredom/subprocess.h) | Unlicense | text |
| [xxHash](https://github.com/Cyan4973/xxHash) | BSD-2-Clause | text |
| rotate-bits | MIT | text |
| SHA-1 (Steve Reid), SHA-256 (Igor Pavlov) | public domain | text |
| [zip](https://github.com/kuba--/zip) and [miniz](https://github.com/richgel999/miniz) | MIT | image |
| [libwebp](https://github.com/webmproject/libwebp) | BSD-3-Clause | image |
| [oniguruma](https://github.com/kkos/oniguruma) | BSD-2-Clause | image |
| [utf8proc](https://github.com/JuliaStrings/utf8proc) | MIT | image |
| [darts-clone](https://github.com/s-yata/darts-clone) | BSD-2-Clause | image |

Where a library keeps its license inside a header (or not in the vendored tree
at all), the notice is kept verbatim in `runtime/notices/<engine>/`, and the
build refuses to run if the pinned source no longer matches it.

The gateway's Python dependencies, including
[Jinja](https://github.com/pallets/jinja) (BSD-3-Clause) for chat template
rendering, are installed from PyPI under their own licenses.

This notice describes provenance; it is not legal advice and does not replace
the license texts distributed by upstream projects.
