# Model catalog

`@models` contains portable model identity only. Files in `catalog/` may be
published and packaged because they contain immutable source coordinates,
artifact metadata, capabilities, and defaults—never absolute paths, device
indexes, tensor splits, credentials, or engine command lines.

ENGRAI resolves catalog artifacts into its XDG data directory, then stores the
host-specific binding under `${XDG_CONFIG_HOME:-~/.config}/engrai-server/deployments/`.
The selected engine adapter compiles that typed deployment into an executable
launch specification. Compiled arguments are output, not source data.

The contracts are:

- `schemas/model-manifest.schema.json`: tracked portable catalog entries.
- `schemas/deployment-profile.schema.json`: ignored, machine-local bindings.
- `catalog/`: public model manifests (empty until reviewed model entries exist).
