# Containers and pods

ENGRAI runs as one application container. The gateway starts and supervises
the pinned text and image workers as child processes, so their loopback-only
ports never become pod ports. The container image contains both verified
runtime bundles and installs them idempotently into persistent state at boot.

## Build an image

The build fetches the exact engine commits in `runtime/*.lock.json`, compiles
both native workers, rebuilds the web UI, and creates a non-root final image.
It can take a while; `BUILD_JOBS` limits parallel compiler memory use.

Portable CPU image:

```bash
docker build \
  --file Containerfile \
  --build-arg ENGRAI_BACKEND=cpu \
  --build-arg BUILD_JOBS=4 \
  --tag engrai-server:0.1.0-cpu \
  .
```

NVIDIA CUDA image:

```bash
docker build \
  --file Containerfile \
  --build-arg BUILD_IMAGE=nvidia/cuda:12.8.1-devel-ubuntu24.04 \
  --build-arg RUNTIME_IMAGE=nvidia/cuda:12.8.1-runtime-ubuntu24.04 \
  --build-arg ENGRAI_BACKEND=cuda \
  --build-arg BUILD_JOBS=4 \
  --tag engrai-server:0.1.0-cuda \
  .
```

The default CUDA build is portable across the architectures enabled by the
upstream toolchain. For a private, faster build, restrict it to the compute
capabilities of the actual nodes, for example:

```bash
--build-arg 'CUDA_ARCHITECTURES=86;89'
```

The CUDA base tags are explicit and can be replaced together when your host
driver and release policy call for another toolkit version. Podman can build
the same `Containerfile`; replace `docker build` with `podman build`.

## Run locally

The Compose example keeps the public port on host loopback and stores models
in a persistent named volume. The web control plane can pull selected files
from Hugging Face directly into that volume:

```bash
docker compose up --build
```

Set `ENGRAI_MODELS_PATH` to a host directory when model weights live elsewhere.
The Compose mount uses `:Z`, which gives that directory a private SELinux label
for rootless Podman on Fedora; because the model puller writes there, it cannot
be mounted read-only. Then open `http://127.0.0.1:8400` and complete first-run
setup.
The state volume contains credentials, deployment profiles, runtime receipts,
logs, and image results; back it up as sensitive data.

For an NVIDIA container outside Kubernetes, provide a CUDA-built image and
explicit GPU access:

```bash
docker run --rm --gpus all \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=1g \
  --publish 127.0.0.1:8400:8400 \
  --volume engrai-state:/var/lib/engrai \
  --volume engrai-models:/models \
  engrai-server:0.1.0-cuda
```

## Kubernetes pod

The Kustomize base creates a single-replica `Deployment`, a private
`ClusterIP` service, and separate state and model claims. `Recreate` is
intentional: two gateways must not concurrently mutate one `ReadWriteOnce`
state volume. The pod runs as UID/GID 10001, drops all capabilities, uses a
read-only root filesystem, and does not receive a Kubernetes API token. A
`NetworkPolicy` admits port 8400 only from same-namespace pods explicitly
labeled `engrai.network/client=true`; adjust that policy for an ingress
controller or mesh. CNIs without NetworkPolicy enforcement ignore it.

Before applying, push the selected image to a registry reachable by the
cluster and set its immutable name or digest in the overlay:

```bash
cd deploy/kubernetes/overlays/nvidia
kustomize edit set image \
  engrai-server=registry.example/engrai-server:0.1.0-cuda
kubectl apply -k .
```

For CPU nodes, use `deploy/kubernetes/overlays/cpu` and the CPU image. Adjust
the two PVC sizes and resource requests for the models you actually serve.
If the cluster already provides model storage, patch the `engrai-models`
claim or the deployment volume before applying.

The NVIDIA overlay requests one `nvidia.com/gpu`. The cluster must already
expose that extended resource through the NVIDIA device plugin or GPU
Operator; the application pod does not install drivers and is never
privileged.

The service is deliberately not public. Reach it for initial setup with:

```bash
kubectl --namespace engrai-system port-forward service/engrai-server 8400:8400
```

Then open `http://127.0.0.1:8400`. If unattended bootstrap is required, create
the optional `engrai-bootstrap` secret before the first pod starts:

```bash
kubectl --namespace engrai-system create secret generic engrai-bootstrap \
  --from-literal=ENGRAI_ADMIN_TOKEN='replace-with-a-long-random-token' \
  --from-literal=ENGRAI_API_KEYS='replace-with-a-different-long-random-key'
```

Those variables seed the credential store only when it is empty. Rotations
after first boot happen through ENGRAI and persist on the state claim.

For private or gated Hub repositories, add a scoped read-only `HF_TOKEN` to
that same secret. ENGRAI uses the standard `huggingface_hub` environment
credential lookup; it never sends the token to the browser or stores it in its
download history. Public repositories need no token. The model claim is
writable by design because Hub cache files and resumable partial downloads
live there.

Use an ingress, private tunnel sidecar, or mesh only after choosing its trust
and authentication boundary. Do not create services for ports 5002 or 5003;
they are private worker endpoints bound to pod loopback.
