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
