import { useState } from "react";
import { useStore } from "../store/useStore";
import { useLiveState } from "../lib/useLiveState";
import { useVersionCheck } from "../lib/useVersionCheck";
import { summarise } from "../lib/engine";
import { Topbar } from "../components/Topbar";
import { Footer } from "../components/Footer";
import { ActiveRoute } from "../components/ActiveRoute";
import { Meters } from "../components/Meters";
import { ModelCard } from "../components/ModelCard";
import { Activity } from "../components/Activity";
import { Endpoint } from "../components/Endpoint";
import { Sheet } from "../components/Sheet";
import { ProfileEditor } from "../components/ProfileEditor";
import { ImageProfileEditor } from "../components/ImageProfileEditor";
import { ImageModelCard } from "../components/ImageModelCard";
import { Account } from "../components/Account";
import { ModelPuller } from "../components/ModelPuller";
import { Button, Empty, StatusDot } from "../components/primitives";

export function Control() {
  useLiveState();

  const snapshot = useStore((s) => s.snapshot);
  const busyModel = useStore((s) => s.busyModel);
  const load = useStore((s) => s.load);
  const busyImage = useStore((s) => s.busyImage);
  const loadImage = useStore((s) => s.loadImage);
  const unloadImage = useStore((s) => s.unloadImage);

  const [editing, setEditing] = useState<string | null>(null);
  const [editingImage, setEditingImage] = useState<string | null>(null);
  const [account, setAccount] = useState(false);
  const [engineSheet, setEngineSheet] = useState(false);

  /* Reloading the page out from under someone editing a deployment in the
     editor would be its own bug, so the upgrade waits for a quiet moment.
     Closing the last sheet makes this true and the check fires again. */
  const idle =
    editing === null && editingImage === null && !account && !engineSheet &&
    busyModel === null && busyImage === null;
  const version = useVersionCheck({ autoReload: idle });

  const models = snapshot?.models ?? [];
  const engine = summarise(snapshot?.engine, snapshot?.runtime);
  // A second load while one is in flight would queue behind the generation
  // lock and look like the interface had frozen.
  const locked = busyModel !== null;
  const imageModels = snapshot?.image_models ?? [];
  const imageLocked = busyImage !== null;
  const activeImage = imageModels.some((model) => model.active);

  return (
    <div className="shell">
      <Topbar onAccount={() => setAccount(true)} onEngine={() => setEngineSheet(true)} />

      {version.stale && (
        <div className="update-bar" role="status">
          <span>
            {version.stuck
              ? "A newer version is deployed but this page keeps loading the old one — a cached copy is pinning it."
              : "A newer version is available."}
          </span>
          <Button variant="primary" onClick={version.reload}>Reload</Button>
        </div>
      )}

      <main className="content">
        <ActiveRoute />
        {/* The rail says what state the machine is in; the meters say how much
            of it is left. Both belong above the catalog, because whether the
            next model fits is a VRAM question. */}
        <Meters />
        <ModelPuller />

        <section className="panel routes">
          <div className="panel-head">
            <div>
              <span className="eyebrow">Routing Catalog</span>
              <h2>Text Models</h2>
            </div>
            <div className="panel-actions">
              <span className="count">{models.length}</span>
              <Button variant="primary" onClick={() => setEditing("")}>
                New Profile
              </Button>
            </div>
          </div>

          {models.length === 0 ? (
            <Empty
              title="No deployments yet. Bind a model to an engine and host policy."
              action={
                <Button variant="primary" onClick={() => setEditing("")}>
                  Create the first route
                </Button>
              }
            />
          ) : (
            <div className="card-grid">
              {models.map((model) => (
                <ModelCard
                  key={model.id}
                  model={model}
                  busy={busyModel === model.id}
                  disabled={locked}
                  onLoad={() => { void load(model.id).catch(() => {}); }}
                  onEdit={() => setEditing(model.id)}
                />
              ))}
            </div>
          )}
        </section>

        <section className="panel routes" data-testid="image-models">
          <div className="panel-head">
            <div>
              <span className="eyebrow">Routing Catalog</span>
              <h2>Image Models</h2>
            </div>
            <div className="panel-actions">
              <span className="count">{imageModels.length}</span>
              {activeImage && (
                <Button
                  variant="ghost"
                  disabled={imageLocked}
                  onClick={() => { void unloadImage().catch(() => {}); }}
                >
                  Unload
                </Button>
              )}
              <Button variant="primary" onClick={() => setEditingImage("")}>
                New Profile
              </Button>
            </div>
          </div>

          {imageModels.length === 0 ? (
            <Empty
              title="No image models yet. Choose one checkpoint, its text encoder, and its VAE."
              action={<Button variant="primary" onClick={() => setEditingImage("")}>Add image model</Button>}
            />
          ) : (
            <div className="card-grid">
              {imageModels.map((model) => (
                <ImageModelCard
                  key={model.id}
                  model={model}
                  busy={busyImage === model.id}
                  disabled={imageLocked}
                  onLoad={() => { void loadImage(model.id).catch(() => {}); }}
                  onEdit={() => setEditingImage(model.id)}
                />
              ))}
            </div>
          )}
        </section>

        <div className="side-by-side">
          <Endpoint />
          <Activity />
        </div>
      </main>

      <Footer />

      <ProfileEditor
        profileId={editing}
        onClose={() => setEditing(null)}
      />
      <ImageProfileEditor
        profileId={editingImage}
        onClose={() => setEditingImage(null)}
      />
      <Account open={account} onClose={() => setAccount(false)} />

      <Sheet
        open={engineSheet}
        onClose={() => setEngineSheet(false)}
        title={engine.label}
        eyebrow="Inference runtime"
        size="sm"
      >
        <div className="engine-sheet">
          <p className="engine-sheet-line">
            <StatusDot tone={engine.tone} pulse={engine.pulse} />
            {engine.detail}
          </p>
          <dl className="stat-rows">
            <div>
              <dt>Reachable</dt>
              <dd>{snapshot?.engine.reachable ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt>Started by ENGRAI SERVER</dt>
              <dd>{snapshot?.engine.owned ? "Yes" : "No"}</dd>
            </div>
            <div><dt>Version</dt><dd>{snapshot?.engine.version ?? "—"}</dd></div>
            <div><dt>PID</dt><dd>{snapshot?.engine.pid ?? "—"}</dd></div>
            <div><dt>Request lane</dt><dd>{snapshot?.locked ? "Busy" : "Open"}</dd></div>
          </dl>
          {snapshot?.engine.capabilities && (
            <section className="capability-status" aria-label="Effective model capabilities">
              <h3>Model-effective features</h3>
              <dl className="stat-rows">
                {Object.entries(snapshot.engine.capabilities).map(([name, capability]) => (
                  <div key={name} title={capability.detail}>
                    <dt>{name.replaceAll("_", " ")}</dt>
                    <dd>{capability.effective ? "Active" : capability.requested ? "Unsupported" : "Off"}</dd>
                  </div>
                ))}
              </dl>
            </section>
          )}
          {snapshot?.engine.error && (
            <p className="engine-sheet-error">{snapshot.engine.error}</p>
          )}
        </div>
      </Sheet>
    </div>
  );
}
