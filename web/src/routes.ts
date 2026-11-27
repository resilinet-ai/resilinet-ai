import { useEffect, useState } from "react";

// The screens of the wireframes (docs/design/rai-wireframes.md), in navigation order.
// Screens whose API is not built yet show what they will hold and the requirement.
export interface Screen {
  id: string;
  label: string;
  built: boolean;
  requirements: string;
  purpose: string;
}

export const SCREENS: Screen[] = [
  { id: "dashboard", label: "Dashboard", built: true, requirements: "FR-11.1, NFR-USE-1",
    purpose: "Pass rates, recovery attainment and open findings, each with its denominator." },
  { id: "runs", label: "Runs", built: true, requirements: "FR-06.1, FR-12.2, FR-13.1",
    purpose: "Runs of a project, their steps and failing rules, the release gate and evidence." },
  { id: "scenarios", label: "Chaos scenarios", built: false, requirements: "FR-07.1, FR-07.2",
    purpose: "Scenario versions, their blast radius and their approvals." },
  { id: "contracts", label: "Contracts & data", built: false, requirements: "FR-03.1, FR-04.1",
    purpose: "Assets, their contracts and their lineage." },
  { id: "approvals", label: "Approvals & gates", built: false, requirements: "FR-10.1, FR-12.1, FR-12.2",
    purpose: "Findings, waivers and the reviewers who decided them." },
  { id: "evidence", label: "Evidence", built: false, requirements: "FR-13.1, FR-13.2",
    purpose: "Signed evidence bundles and how to verify them." },
  { id: "environments", label: "Environments", built: false, requirements: "FR-07.2, NFR-SAF-1",
    purpose: "Where faults may run, and the target allow-list (admins)." },
  { id: "recovery", label: "Recovery", built: true, requirements: "FR-08.1, FR-08.2, FR-08.3",
    purpose: "RTO and RPO attainment against each objective, and consistency after recovery." },
  { id: "models", label: "Models & drift", built: false, requirements: "FR-09.1, FR-09.2",
    purpose: "Model checks and drift." },
];

export interface Route {
  screen: string;
  id: string | null; // e.g. a run id: #/runs/<id>
}

export function parseHash(hash: string): Route {
  const [screen = "", id = null] = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const known = SCREENS.some((s) => s.id === screen);
  return { screen: known ? screen : "dashboard", id: known ? id : null };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const changed = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  return route;
}
