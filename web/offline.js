"use strict";

// IndexedDB store wrapper for the BridgeSAT offline client.
// Stores per SYNC_PROTOCOL.md section 3:
//   profile_snapshot, active_session, content_packs, pending_events,
//   acknowledged_events, memory_snapshot, sync_state
// Each store keeps a single row keyed by "state" except content_packs
// (keyed by pack_version) and pending/acknowledged events (keyed by
// event_id).

const DB_NAME = "bridgesat-offline";
const DB_VERSION = 1;

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      const stores = [
        "profile_snapshot",
        "active_session",
        "content_packs",
        "pending_events",
        "acknowledged_events",
        "memory_snapshot",
        "sync_state",
      ];
      for (const name of stores) {
        if (!db.objectStoreNames.contains(name)) {
          db.createObjectStore(name, { keyPath: "id" });
        }
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function transaction(db, storeName, mode, fn) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const store = tx.objectStore(storeName);
    let result;
    tx.oncomplete = () => resolve(result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
    const done = (value) => {
      result = value;
    };
    Promise.resolve(fn(store, done)).catch(reject);
  });
}

class OfflineStore {
  constructor(db) {
    this.db = db;
  }

  static async open() {
    return new OfflineStore(await openDatabase());
  }

  async get(storeName, id) {
    return transaction(this.db, storeName, "readonly", (store, done) => {
      const request = store.get(id);
      request.onsuccess = () => done(request.result);
    });
  }

  async put(storeName, record) {
    await transaction(this.db, storeName, "readwrite", (store) => {
      store.put(record);
    });
  }

  async delete(storeName, id) {
    await transaction(this.db, storeName, "readwrite", (store) => {
      store.delete(id);
    });
  }

  async all(storeName) {
    return transaction(this.db, storeName, "readonly", (store, done) => {
      const request = store.getAll();
      request.onsuccess = () => done(request.result || []);
    });
  }
}

// Convenience accessors for the sync-state row.
class SyncStateAccess {
  constructor(store) {
    this.store = store;
  }

  async load() {
    return (await this.store.get("sync_state", "state")) || null;
  }

  async save(state) {
    await this.store.put("sync_state", { id: "state", ...state });
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { OfflineStore, SyncStateAccess };
}
