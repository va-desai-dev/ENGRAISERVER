# Third-Party Notices
ENGRAISERVER values collaboration and transparency. It also values integrity and provenance to ensure fair authorship credit. Accordingly, the following documentation outlines all acknowledgments, third-party frameworks, libraries, and derivative provenance used by ENGRAISERVER. 

## Derivative Provenance
ENGRAISERVER builds its runtimes from pinned upstream sources. The exact tag, commit, build options, and payload checksums are recorded in each runtime bundle, and the upstream license texts are distributed inside every bundle under `licenses/`.
| Service | Upstream | License |
| --- | --- | --- |
| `engrai-text` | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT |
| `engrai-image` | [leejet/stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) | MIT |
|`card-studio`| [(c) 2025 Tremontaine](https://github.com/zebede1980/CardGenV2) | MIT |

## Upstream Frameworks
SymLink uses [noiseprotocol](https://github.com/plizonczyk/noiseprotocol) (MIT),
[cryptography](https://github.com/pyca/cryptography) (Apache-2.0 or BSD-3-Clause),
and [qrcode](https://github.com/lincolnloop/python-qrcode) (BSD-3-Clause).
These local libraries implement the Noise channel and QR rendering; no hosted
networking account or service is introduced by them. Their package metadata and
license files are included with the standalone Python distribution.

Existing frameworks also include libraries from their upstream vendors. Each license text ships in the bundle’s `licenses/` directory:
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
