// Files the UI saves or reads: handing a download to the browser, and reading an evidence
// bundle's members.

/** Hand a file to the browser's own download. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

const EOCD = 0x06054b50; // end of central directory
const CENTRAL = 0x02014b50;
const LOCAL = 0x04034b50;

/** The members of a ZIP file that are stored, not compressed, by path. An evidence bundle
 * stores every member (docs/api.md, "Deterministic"), so no inflater is needed; a
 * compressed member is skipped. Throws on anything that is not a ZIP file. */
export function storedMembers(data: ArrayBuffer): Map<string, Uint8Array> {
  const view = new DataView(data);
  let end = -1;
  for (let i = data.byteLength - 22; i >= Math.max(0, data.byteLength - 22 - 0xffff); i--) {
    if (view.getUint32(i, true) === EOCD) {
      end = i;
      break;
    }
  }
  if (end < 0) throw new Error("not a ZIP file");
  const count = view.getUint16(end + 10, true);
  let at = view.getUint32(end + 16, true);
  const names = new TextDecoder();
  const out = new Map<string, Uint8Array>();
  for (let n = 0; n < count; n++) {
    if (view.getUint32(at, true) !== CENTRAL) throw new Error("damaged ZIP directory");
    const method = view.getUint16(at + 10, true);
    const size = view.getUint32(at + 20, true);
    const nameLength = view.getUint16(at + 28, true);
    const extraLength = view.getUint16(at + 30, true);
    const commentLength = view.getUint16(at + 32, true);
    const local = view.getUint32(at + 42, true);
    const name = names.decode(new Uint8Array(data, at + 46, nameLength));
    if (method === 0) {
      if (view.getUint32(local, true) !== LOCAL) throw new Error("damaged ZIP entry");
      const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
      out.set(name, new Uint8Array(data, start, size));
    }
    at += 46 + nameLength + extraLength + commentLength;
  }
  return out;
}

/** A member under the bundle's one folder, parsed as JSON, or null. */
export function memberJson<T>(members: Map<string, Uint8Array>, file: string): T | null {
  for (const [path, bytes] of members) {
    if (path.split("/").slice(1).join("/") === file) {
      return JSON.parse(new TextDecoder().decode(bytes)) as T;
    }
  }
  return null;
}
