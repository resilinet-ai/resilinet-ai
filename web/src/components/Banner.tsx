import type { Project } from "../api";

// FR-11.2 (B-37): a persistent synthetic-data banner. It is on every screen, sign-in
// included, and cannot be dismissed. A project marked as an authorised snapshot says
// so, because its data is a copy of real data under an authorisation, not synthetic.
export function Banner({ project }: { project?: Project | null }) {
  const snapshot = project?.data_classification === "authorized_snapshot";
  return (
    <div className={snapshot ? "banner snapshot" : "banner"} role="note" aria-label="Data notice">
      RAI RUNS ON SANDBOX AND STAGING DATA ONLY · Never production ·{" "}
      {snapshot
        ? `Project ${project.name} uses an authorised snapshot`
        : "Synthetic or authorised-snapshot data"}
    </div>
  );
}
