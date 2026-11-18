import type { ReactNode } from "react";
import type { Me, Project, Tenant } from "../api";
import { SCREENS, type Route } from "../routes";
import type { ThemeId } from "../theme";
import { Banner } from "./Banner";
import { Brand } from "./Brand";
import { ThemePicker } from "./ThemePicker";

interface Props {
  me: Me;
  tenant: Tenant;
  route: Route;
  projects: Project[];
  project: Project | null;
  onProject: (id: string) => void;
  theme: ThemeId;
  onTheme: (t: ThemeId) => void;
  onSignOut: () => void;
  children: ReactNode;
}

function initials(name: string): string {
  return name.split(/\s+/).map((part) => part[0] ?? "").join("").slice(0, 2).toUpperCase();
}

export function Shell(props: Props) {
  const { me, tenant, route, projects, project } = props;
  return (
    <div className="app">
      <a className="skip" href="#main">Skip to content</a>
      <nav className="side" aria-label="Main">
        <Brand />
        {SCREENS.map((s) => (
          <a key={s.id} className="nav" href={`#/${s.id}`} aria-current={route.screen === s.id ? "page" : undefined}>
            {s.label}
            {!s.built && <span className="soon">planned</span>}
          </a>
        ))}
      </nav>
      <div className="col">
        <header className="top">
          <div className="crumbs">
            <b>{tenant.name}</b>
            <span className="dim">/</span>
            <label className="inline-field">
              <span className="sr-only">Project</span>
              <select className="btn sm" value={project?.project_id ?? ""}
                onChange={(e) => props.onProject(e.target.value)} disabled={projects.length === 0}>
                {projects.length === 0 && <option value="">No projects</option>}
                {projects.map((p) => (
                  <option key={p.project_id} value={p.project_id}>{p.name}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="who">
            <ThemePicker theme={props.theme} onChange={props.onTheme} />
            <span className="avatar" aria-hidden="true">{initials(me.display_name)}</span>
            <span>
              {me.display_name} <span className="dim">· {tenant.role}</span>
            </span>
            <button type="button" className="btn sm" onClick={props.onSignOut}>Sign out</button>
          </div>
        </header>
        <Banner project={project} />
        <main id="main" tabIndex={-1}>{props.children}</main>
      </div>
    </div>
  );
}
