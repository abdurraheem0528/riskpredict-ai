/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  readonly VITE_API_BASE_URL?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
/** Hash of public/map/{centroids,ports}.json, set by vite.config.ts. */
declare const __MAP_DATA_VERSION__: string;
