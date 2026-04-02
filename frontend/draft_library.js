(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.ReportDraftLibrary = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  const DB_NAME = "report_automation_versions_v2";
  const DB_VERSION = 1;
  const STORE_NAME = "drafts";

  function cloneSnapshot(value) {
    if (typeof structuredClone === "function") {
      return structuredClone(value);
    }
    return JSON.parse(JSON.stringify(value));
  }

  function normalizeCompanyName(rawCompanyName) {
    const text = String(rawCompanyName || "").trim();
    if (!text) {
      throw new Error("企业名称不能为空");
    }
    return text;
  }

  function toDisplayCompanyName(rawCompanyName) {
    const text = String(rawCompanyName || "").trim();
    return text || "未选择企业";
  }

  function normalizeVersionNo(versionNo) {
    const parsed = Number(versionNo);
    if (!Number.isInteger(parsed) || parsed < 1) {
      throw new Error("版本号必须是大于等于 1 的整数");
    }
    return parsed;
  }

  function makeDraftId(companyName, kind, versionNo) {
    if (kind !== "version") {
      throw new Error("仅支持版本草稿");
    }
    const normalizedCompany = normalizeCompanyName(companyName);
    const normalizedVersionNo = normalizeVersionNo(versionNo);
    return `${encodeURIComponent(normalizedCompany)}::version::${normalizedVersionNo}`;
  }

  class IndexedDbDraftStore {
    constructor(options = {}) {
      this.indexedDB = options.indexedDB || (typeof indexedDB !== "undefined" ? indexedDB : null);
      this.dbName = options.dbName || DB_NAME;
      this.dbVersion = options.dbVersion || DB_VERSION;
      this.storeName = options.storeName || STORE_NAME;
      this._dbPromise = null;
    }

    async open() {
      if (!this.indexedDB) {
        throw new Error("当前浏览器不支持 IndexedDB");
      }
      if (this._dbPromise) {
        return this._dbPromise;
      }

      this._dbPromise = new Promise((resolve, reject) => {
        const request = this.indexedDB.open(this.dbName, this.dbVersion);

        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains(this.storeName)) {
            const store = db.createObjectStore(this.storeName, { keyPath: "id" });
            store.createIndex("companyName", "companyName", { unique: false });
            store.createIndex("kind", "kind", { unique: false });
            store.createIndex("savedTs", "savedTs", { unique: false });
          }
        };

        request.onsuccess = () => {
          resolve(request.result);
        };

        request.onerror = () => {
          reject(request.error || new Error("IndexedDB 打开失败"));
        };
      });

      return this._dbPromise;
    }

    async put(record) {
      const db = await this.open();
      await new Promise((resolve, reject) => {
        const tx = db.transaction(this.storeName, "readwrite");
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error("IndexedDB 写入失败"));
        tx.onabort = () => reject(tx.error || new Error("IndexedDB 写入中止"));
        tx.objectStore(this.storeName).put(record);
      });
    }

    async get(id) {
      const db = await this.open();
      return new Promise((resolve, reject) => {
        const tx = db.transaction(this.storeName, "readonly");
        tx.onerror = () => reject(tx.error || new Error("IndexedDB 读取失败"));
        const request = tx.objectStore(this.storeName).get(id);
        request.onsuccess = () => resolve(request.result || null);
        request.onerror = () => reject(request.error || new Error("IndexedDB 读取失败"));
      });
    }

    async delete(id) {
      const db = await this.open();
      await new Promise((resolve, reject) => {
        const tx = db.transaction(this.storeName, "readwrite");
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error || new Error("IndexedDB 删除失败"));
        tx.onabort = () => reject(tx.error || new Error("IndexedDB 删除中止"));
        tx.objectStore(this.storeName).delete(id);
      });
    }

    async getAll() {
      const db = await this.open();
      return new Promise((resolve, reject) => {
        const tx = db.transaction(this.storeName, "readonly");
        tx.onerror = () => reject(tx.error || new Error("IndexedDB 列表读取失败"));
        const request = tx.objectStore(this.storeName).getAll();
        request.onsuccess = () => resolve(Array.isArray(request.result) ? request.result : []);
        request.onerror = () => reject(request.error || new Error("IndexedDB 列表读取失败"));
      });
    }
  }

  class InMemoryDraftStore {
    constructor() {
      this.records = new Map();
    }

    async put(record) {
      this.records.set(record.id, cloneSnapshot(record));
    }

    async get(id) {
      const value = this.records.get(id);
      return value ? cloneSnapshot(value) : null;
    }

    async delete(id) {
      this.records.delete(id);
    }

    async getAll() {
      return Array.from(this.records.values()).map((item) => cloneSnapshot(item));
    }
  }

  class DraftLibrary {
    constructor(options = {}) {
      const customStore = options.store || null;
      this.store = customStore || new IndexedDbDraftStore(options);
      this.now = typeof options.now === "function" ? options.now : () => Date.now();
    }

    async init() {
      if (typeof this.store.open === "function") {
        await this.store.open();
      }
    }

    _createRecord(companyName, versionNo, snapshot, savedTs) {
      if (!snapshot || typeof snapshot !== "object") {
        throw new Error("草稿数据格式不正确");
      }
      const normalizedCompanyName = normalizeCompanyName(companyName);
      const normalizedVersionNo = normalizeVersionNo(versionNo);
      const normalizedSavedTs = Number(savedTs) > 0 ? Number(savedTs) : this.now();

      return {
        id: makeDraftId(normalizedCompanyName, "version", normalizedVersionNo),
        companyName: normalizedCompanyName,
        kind: "version",
        versionNo: normalizedVersionNo,
        snapshot: cloneSnapshot(snapshot),
        savedTs: normalizedSavedTs,
      };
    }

    async saveVersionDraft(companyName, versionNo, snapshot, savedTs) {
      const record = this._createRecord(companyName, versionNo, snapshot, savedTs);
      await this.store.put(record);
      return record;
    }

    async getDraft(companyName, kind, versionNo) {
      if (kind !== "version") {
        throw new Error("仅支持读取版本草稿");
      }
      const id = makeDraftId(companyName, kind, versionNo);
      return this.store.get(id);
    }

    async deleteVersionDraft(companyName, versionNo) {
      const id = makeDraftId(companyName, "version", versionNo);
      await this.store.delete(id);
    }

    async deleteCompany(companyName) {
      const normalizedCompanyName = normalizeCompanyName(companyName);
      const all = await this.store.getAll();
      const targets = all.filter((item) => item.companyName === normalizedCompanyName);
      for (const item of targets) {
        await this.store.delete(item.id);
      }
      return targets.length;
    }

    async listCompanyDrafts(companyName) {
      const normalizedCompanyName = normalizeCompanyName(companyName);
      const all = await this.store.getAll();
      return all
        .filter((item) => item.companyName === normalizedCompanyName && item.kind === "version")
        .sort((a, b) => {
          if (a.versionNo !== b.versionNo) {
            return a.versionNo - b.versionNo;
          }
          return b.savedTs - a.savedTs;
        });
    }

    async listCompanies() {
      const all = await this.store.getAll();
      const versionRecords = all.filter((item) => item.kind === "version");
      const grouped = new Map();
      for (const item of versionRecords) {
        const previous = grouped.get(item.companyName) || {
          companyName: item.companyName,
          latestTs: 0,
          versionCount: 0,
        };
        previous.latestTs = Math.max(previous.latestTs, Number(item.savedTs) || 0);
        previous.versionCount += 1;
        grouped.set(item.companyName, previous);
      }
      return Array.from(grouped.values()).sort((a, b) => {
        if (b.latestTs !== a.latestTs) {
          return b.latestTs - a.latestTs;
        }
        return a.companyName.localeCompare(b.companyName, "zh-CN");
      });
    }

    async getNextVersionNo(companyName) {
      const items = await this.listCompanyDrafts(companyName);
      if (!items.length) return 1;
      const maxVersionNo = Math.max(...items.map((item) => Number(item.versionNo) || 0));
      return maxVersionNo + 1;
    }
  }

  function createBrowserDraftLibrary(options = {}) {
    return new DraftLibrary({
      ...options,
      indexedDB: options.indexedDB || (typeof indexedDB !== "undefined" ? indexedDB : null),
    });
  }

  return {
    DB_NAME,
    DB_VERSION,
    STORE_NAME,
    normalizeCompanyName,
    toDisplayCompanyName,
    makeDraftId,
    DraftLibrary,
    IndexedDbDraftStore,
    InMemoryDraftStore,
    createBrowserDraftLibrary,
  };
});
