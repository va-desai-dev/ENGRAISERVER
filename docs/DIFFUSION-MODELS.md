# Diffusion deployments

Diffusion models use the same catalog and deployment pipeline as text models.
A portable manifest declares artifact roles such as checkpoint, text encoder,
VAE, and optional adapters. A local deployment chooses the engine, accelerator,
device, memory policy, tiling, and offload behavior.

Generation settings such as prompt, negative prompt, seed, dimensions, steps,
guidance, sampler, and scheduler belong to the request or typed defaults—not
to an upstream command string.

The OpenAI-compatible `/v1/images/generations` endpoint runs on the
`engrai-image` runtime: stable-diffusion.cpp's `sd-server`, pinned in
`runtime/stable-diffusion.cpp.lock.json` and built into a verified bundle. The
worker binds to loopback only; clients use ENGRAI and never connect to an
engine port. Without an installed image runtime, image requests fail with
install instructions rather than falling back to another engine.

Image profiles currently describe the FLUX.2 Klein layout: a diffusion model
and VAE on one GPU, and a Qwen-style text encoder on the CPU. Profiles that
describe each component by role, for model families with other encoders, are
planned.
