import type { Screen } from "../routes";

export function Planned({ screen }: { screen: Screen }) {
  return (
    <section className="screen" aria-labelledby="planned-h">
      <div className="head">
        <div>
          <h1 id="planned-h">{screen.label}</h1>
          <p className="sub">{screen.purpose}</p>
        </div>
        <span className="pill warn">◷ Planned</span>
      </div>
      <div className="card">
        <p>
          This screen is not built yet. Its design is wireframe {screen.label} in{" "}
          <span className="mono">docs/design/rai-wireframes.md</span>.
        </p>
        <span className="tag">{screen.requirements}</span>
      </div>
    </section>
  );
}
