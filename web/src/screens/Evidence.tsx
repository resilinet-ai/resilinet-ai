import { useEffect, useState } from "react";
import { ApiError, type Api, type BundleInfo, type Page, type Project, type PublicKey, type Run } from "../api";
import { memberJson, saveBlob, storedMembers } from "../files";
import { shortId } from "./Runs";

// W06: evidence (FR-13.1, FR-13.2, NFR-AUD-2). Export a finished run's bundle, see what
// it holds and what it does not show, and how to check it outside RAI with `rai verify`.

const FINISHED = new Set(["passed", "failed", "refused", "aborted", "error"]);

// docs/api.md, "Evidence": the members every bundle has, in its order.
const CONTENTS = [
  "The run and the pins it executed from",
  "Every step and observation, with the values observed and expected",
  "Lineage edges, with the SHA-256 digest of the data read",
  "Recovery measurements, for chaos runs",
  "The scenario approvals the run was pinned to",
  "The run's audit-chain entries, with their hashes",
  "A report for a person to read, with its limitations",
  "A SHA-256 manifest of every member",
  "An Ed25519 signature over the manifest, when the server has a signing key",
];

const COUNTS: [string, string][] = [
  ["steps", "steps"], ["observations", "observations"], ["lineage_edges", "lineage edges"],
  ["recovery_measurements", "recovery measurements"], ["scenario_approvals", "approvals"],
  ["audit_entries", "audit entries"],
];

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : "The RAI API cannot be reached.";
}

/** "8d2c…7b": a hash short enough to read, long enough to compare by eye. */
export function short(hash: string): string {
  return hash.length > 12 ? `${hash.slice(0, 6)}…${hash.slice(-4)}` : hash;
}

export function size(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${Math.round(bytes / 1024)} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

interface Export {
  filename: string;
  bytes: number;
  manifestSha256: string | null;
  keyId: string | null;
  files: number | null;
  info: BundleInfo | null;
}

async function inspect(blob: Blob, filename: string, headers: Headers): Promise<Export> {
  const out: Export = {
    filename, bytes: blob.size, manifestSha256: headers.get("X-RAI-Manifest-SHA256"),
    keyId: headers.get("X-RAI-Signature-Key-Id"), files: null, info: null,
  };
  try {
    const members = storedMembers(await blob.arrayBuffer());
    out.info = memberJson<BundleInfo>(members, "bundle.json");
    out.files = memberJson<{ files: unknown[] }>(members, "manifest.json")?.files.length ?? null;
  } catch {
    // not readable here: the file is saved all the same, and rai verify reports what is wrong
  }
  return out;
}

export function Evidence({ api, project, runId }: { api: Api; project: Project | null; runId: string | null }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [chosen, setChosen] = useState<string>("");
  const [key, setKey] = useState<PublicKey | null | undefined>(undefined); // null: unsigned
  const [last, setLast] = useState<Export | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!project) return;
    let live = true;
    setRuns(null);
    setLast(null);
    api.get<Page<Run>>(`/v1/assurance/runs?project_id=${project.project_id}&limit=50`)
      .then((page) => {
        if (!live) return;
        const finished = page.items.filter((r) => FINISHED.has(r.state));
        setRuns(finished);
        setChosen(finished.some((r) => r.run_id === runId) ? runId! : finished[0]?.run_id ?? "");
      })
      .catch((caught: unknown) => live && setError(message(caught)));
    return () => {
      live = false;
    };
  }, [api, project, runId]);

  useEffect(() => {
    let live = true;
    api.get<PublicKey>("/v1/evidence/public-key")
      .then((k) => live && setKey(k))
      .catch((caught: unknown) => {
        if (!live) return;
        setKey(caught instanceof ApiError && caught.problem.code === "evidence.unsigned" ? null : undefined);
      });
    return () => {
      live = false;
    };
  }, [api]);

  async function exportBundle() {
    setBusy(true);
    setError(null);
    try {
      const { blob, filename, headers } = await api.download(`/v1/assurance/runs/${chosen}/evidence`);
      saveBlob(blob, filename);
      setLast(await inspect(blob, filename, headers));
    } catch (caught) {
      setError(message(caught));
    } finally {
      setBusy(false);
    }
  }

  if (!project) {
    return (
      <section className="screen">
        <h1>Evidence</h1>
        <p className="note">This tenant has no projects yet. Create one through the API (docs/api.md).</p>
      </section>
    );
  }

  return (
    <section className="screen" aria-labelledby="evidence-h">
      <div className="head">
        <div>
          <h1 id="evidence-h">Evidence</h1>
          <p className="sub">Deterministic bundles for review outside RAI</p>
        </div>
      </div>
      {error && <div className="box alert" role="alert">{error}</div>}
      <div className="grid g2">
        <article className="card vstack" aria-labelledby="export-h">
          <div className="row"><h2 id="export-h">Export a bundle</h2><span className="tag">FR-13.1</span></div>
          {runs === null ? (
            !error && <p className="note" role="status">Loading runs…</p>
          ) : runs.length === 0 ? (
            <p className="note">No finished run in this project yet. A run has evidence once it finishes.</p>
          ) : (
            <>
              <label className="field">
                <span>Run</span>
                <select value={chosen} onChange={(e) => setChosen(e.target.value)}>
                  {runs.map((r) => (
                    <option key={r.run_id} value={r.run_id}>
                      {shortId(r.run_id)} · {r.state} · finished {(r.finished_at ?? "").slice(0, 16).replace("T", " ")} UTC
                    </option>
                  ))}
                </select>
              </label>
              <div>
                <h3 className="lbl">Contents (always included)</h3>
                <ul className="small">{CONTENTS.map((c) => <li key={c}>{c}</li>)}</ul>
              </div>
              <div className="row">
                <button type="button" className="btn primary" disabled={busy || !chosen}
                  onClick={() => void exportBundle()}>
                  {busy ? "Exporting…" : "Export bundle"}
                </button>
                <a className="small" href={`#/runs/${chosen}`}>Open the run</a>
              </div>
              <p className="small">
                Synthetic data and secret values are not included: the bundle holds the data's
                digests and the secrets' references. Two exports of the same run are
                byte-identical, and each export is recorded in the audit chain.
              </p>
            </>
          )}
        </article>
        <LastExport last={last} keyInfo={key} />
      </div>
    </section>
  );
}

function LastExport({ last, keyInfo }: { last: Export | null; keyInfo: PublicKey | null | undefined }) {
  const keyArg = keyInfo ? `--key ${keyInfo.public_key}` : keyInfo === null ? "--allow-unsigned" : "--key <public key>";
  const signedBy = last?.keyId ?? last?.info?.signature?.key_id ?? null;
  return (
    <article className="card vstack" aria-labelledby="last-h">
      <div className="row"><h2 id="last-h">Last export</h2><span className="tag">FR-13.2 · NFR-AUD-2</span></div>
      {!last ? (
        <p className="note">Nothing exported yet in this session.</p>
      ) : (
        <>
          <dl className="kv">
            <dt>File</dt><dd><span className="mono">{last.filename}</span> · {size(last.bytes)}</dd>
            <dt>Manifest</dt>
            <dd className="mono" title={last.manifestSha256 ?? undefined}>
              {last.manifestSha256 ? `sha256 ${short(last.manifestSha256)}` : "—"}
              {last.files !== null && ` · ${last.files} files`}
            </dd>
            <dt>Anchor</dt>
            <dd className="mono" title={last.info?.anchor.record_hash}>
              {last.info ? `audit entry seq ${last.info.anchor.seq.toLocaleString()} · ${short(last.info.anchor.record_hash)}` : "—"}
            </dd>
            <dt>Signature</dt>
            <dd>{signedBy ? <span className="mono">Ed25519 · key {signedBy}</span>
              : <span className="pill warn">▲ unsigned</span>}</dd>
            {last.info && (
              <>
                <dt>Holds</dt>
                <dd>{COUNTS.map(([k, label]) => `${last.info!.counts[k] ?? 0} ${label}`).join(" · ")}</dd>
              </>
            )}
          </dl>
          {last.info && (
            <div>
              <h3 className="lbl">What this evidence does not show</h3>
              <ul className="small">{last.info.limitations.map((l) => <li key={l}>{l}</li>)}</ul>
            </div>
          )}
          <div>
            <h3 className="lbl">Check it outside RAI</h3>
            <pre className="sunk mono" aria-label="Verify command">rai verify {last.filename} {keyArg}</pre>
            <p className="small">
              Exit 0 means every member matches the manifest{keyInfo === null ? "" : " and the signature is by that key"}.
              The anchor is printed, not checked: compare it with the live chain (<span className="mono">rai audit verify</span>).
            </p>
          </div>
          {keyInfo && signedBy && signedBy !== keyInfo.key_id && (
            <p className="box" role="note">▲ This bundle is signed by key {signedBy}; the server now signs with {keyInfo.key_id}.</p>
          )}
        </>
      )}
      <div>
        <h3 className="lbl">Signing key</h3>
        {keyInfo === undefined ? (
          <p className="note">The signing key cannot be read now.</p>
        ) : keyInfo === null ? (
          <p className="note">▲ This server does not sign bundles: <span className="mono">rai verify</span> checks the
            manifest only, with <span className="mono">--allow-unsigned</span>.</p>
        ) : (
          <>
            <dl className="kv">
              <dt>Key id</dt><dd className="mono">{keyInfo.key_id}</dd>
              <dt>Public key</dt><dd className="mono anywhere">{keyInfo.public_key}</dd>
            </dl>
            <p className="small">
              Pin the key from a source you trust, such as your release notes, not only from this
              server: anyone who edits a bundle can sign it again with their own key.
            </p>
          </>
        )}
      </div>
      <p className="box">
        {last?.info?.statement ??
          "Tamper-evident, not tamper-proof: a change to the bundle or the audit chain is detected, not prevented."}
      </p>
    </article>
  );
}
