import "@testing-library/jest-dom/vitest";

// Node 25 has its own experimental localStorage, which shadows jsdom's and does not work
// without a file. Tests get a plain in-memory Storage instead, whatever the Node version.
class MemoryStorage implements Storage {
  private items = new Map<string, string>();
  get length() {
    return this.items.size;
  }
  clear() {
    this.items.clear();
  }
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  key(index: number) {
    return [...this.items.keys()][index] ?? null;
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
  setItem(key: string, value: string) {
    this.items.set(key, String(value));
  }
}

Object.defineProperty(globalThis, "localStorage", { value: new MemoryStorage(), configurable: true });
Object.defineProperty(window, "localStorage", { value: globalThis.localStorage, configurable: true });
