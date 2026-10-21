import type { CloudCatalog } from './types'

/** All App catalog publishers share this clock, including settings and bootstrap. */
export function createCatalogGate() {
  let epoch = 0
  let latest: CloudCatalog | undefined
  return {
    capture: () => epoch,
    latest: () => latest,
    publish(catalog: CloudCatalog, expectedEpoch?: number): boolean {
      if (expectedEpoch !== undefined && expectedEpoch !== epoch) return false
      latest = catalog; epoch++
      return true
    },
  }
}

export interface CatalogRefreshEntry { published: boolean }

export function beginCatalogRefresh(entries: Map<string, CatalogRefreshEntry>, signature: string): CatalogRefreshEntry | null {
  if (entries.has(signature)) return null
  const entry = { published: false }
  entries.set(signature, entry)
  return entry
}

/** A cancelled old read cannot remove the entry of a later read for the same job. */
export function releaseUnpublishedRefresh(entries: Map<string, CatalogRefreshEntry>, signature: string, entry: CatalogRefreshEntry) {
  if (!entry.published && entries.get(signature) === entry) entries.delete(signature)
}
