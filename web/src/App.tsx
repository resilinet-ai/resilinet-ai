import { useCallback, useEffect, useMemo, useState } from "react";
import { Api, type Me, type Page, type Project, type Tenant } from "./api";
import { Banner } from "./components/Banner";
import { Shell } from "./components/Shell";
import { ThemePicker } from "./components/ThemePicker";
import { SCREENS, useRoute } from "./routes";
import { Dashboard } from "./screens/Dashboard";
import { Planned } from "./screens/Planned";
import { Runs } from "./screens/Runs";
import { SignIn } from "./screens/SignIn";
import { applyTheme, savedTheme, type ThemeId } from "./theme";

interface Session {
  me: Me;
  tenant: Tenant;
}

export function App({ api: given }: { api?: Api }) {
  const api = useMemo(() => given ?? new Api(), [given]);
  const [theme, setTheme] = useState<ThemeId>(savedTheme);
  const [session, setSession] = useState<Session | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const route = useRoute();

  useEffect(() => applyTheme(theme), [theme]);

  const signedIn = useCallback(
    (me: Me, tenant: Tenant) => {
      api.tenantId = tenant.tenant_id;
      setSession({ me, tenant });
    },
    [api],
  );

  useEffect(() => {
    if (!session) return;
    let live = true;
    api
      .get<Page<Project>>("/v1/projects?limit=200")
      .then((page) => {
        if (!live) return;
        const active = page.items.filter((p) => p.status === "active");
        setProjects(active);
        setProjectId((current) => current ?? active[0]?.project_id ?? null);
      })
      .catch(() => live && setProjects([]));
    return () => {
      live = false;
    };
  }, [api, session]);

  async function signOut() {
    await api.logout();
    setSession(null);
    setProjects([]);
    setProjectId(null);
  }

  if (!session) {
    return (
      <div className="center">
        <Banner />
        <div className="signin">
          <SignIn api={api} onSignedIn={signedIn} />
        </div>
        <div className="signin-foot">
          <ThemePicker theme={theme} onChange={setTheme} />
        </div>
      </div>
    );
  }

  const project = projects.find((p) => p.project_id === projectId) ?? null;
  const screen = SCREENS.find((s) => s.id === route.screen) ?? SCREENS[0]!;
  return (
    <Shell me={session.me} tenant={session.tenant} route={route} projects={projects} project={project}
      onProject={setProjectId} theme={theme} onTheme={setTheme} onSignOut={() => void signOut()}>
      {screen.id === "dashboard" ? (
        <Dashboard api={api} project={project} />
      ) : screen.id === "runs" ? (
        <Runs api={api} project={project} runId={route.id} />
      ) : (
        <Planned screen={screen} />
      )}
    </Shell>
  );
}
