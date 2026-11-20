# Model manifests and deployments

ENGRAI separates portable model identity from one workstation's deployment.

Portable manifests live under `@models/catalog/` and conform to
`@models/schemas/model-manifest.schema.json`. They identify immutable source
artifacts, roles, hashes, capabilities, prompt policy, limits, format,
quantization, and license. They cannot contain absolute paths, GPU assignments,
or executable flags.

Local deployment profiles conform to
`@models/schemas/deployment-profile.schema.json` and live under the user's XDG
configuration directory. They select an engine/backend, devices, GPU layers,
split mode, main GPU, tensor allocation, context, KV-cache format/offload,
prompt-cache reuse, context shifting, slot count, generation defaults, and
optional unmodeled compatibility arguments.

The engine adapter combines those records with resolved artifact paths and
emits the provider launch specification. Users edit typed controls; a compiled
command may be shown read-only for diagnostics.

`auto` means "ask ENGRAI's planner," never "let the engine improvise during
launch." The direct llama.cpp compiler accepts only a resolved backend and GPU
layer count. More than one selected accelerator also requires one explicit
tensor-split value per device. This is especially important for machines with
unequal VRAM sizes.

Each route stored under the XDG config `routes/` directory names a local model
file, a display name, and a prompt policy (template source, thinking). Its
deployment has the same ID. Routes and deployments are created in the web UI;
they are never seeded into a new installation or packaged as catalog data.

Routes may also specify an optional `mmproj_path`, an absolute path to the
matching multimodal projector GGUF. Existing schema-version-2 text routes need
no migration. The model and projector must be different files; both must exist
before loading. ENGRAI validates paths and availability, while the worker
validates the actual model/projector architecture pairing at load time.

The profile API (`PUT /control/profiles/{id}`) accepts `mmproj_path` alongside
the existing model and deployment fields. Omitting it preserves an existing
attachment, including when the current web editor saves a profile. Explicit
`null` detaches it. `GET /control/profiles/{id}` returns the attachment and lists
missing files. The web editor offers an optional projector picker with a
custom-path option and a "None" choice to detach it. The standard Hub variant
browser still excludes projectors; use an existing local file or the advanced
download filters.

The launcher passes the resolved projector as `--mmproj`; CPU deployments add
`--no-mmproj-offload`. GPU deployments use the worker's projector placement
defaults. Projector paths cannot be overridden through advanced arguments.
Image/audio/video content uses the worker's native chat handler; available
modalities depend on the chosen model and projector. Adding a projector does
not make a text-only model multimodal or enable function calling.

Tool definitions, tool controls, and tool-result history use the native chat
handler, including follow-up requests that omit `tools`. Ordinary text-only
chat retains ENGRAI's prompt rendering and reasoning splitting. Tool calling
requires Jinja and a suitable model/template; clients execute the returned
calls. The pinned worker accepts string `tool_choice` values (`auto`, `none`,
`required`); forcing a named function with an object remains unsupported.
