"use client";

import { useCallback, useEffect, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_RECOUP_API || "http://127.0.0.1:8000";
// Local development bakes the demo key in. A deployed build bakes nothing: the person
// enters their own key, which lives in this tab's sessionStorage only. (Real deployments
// would put SSO in front of the API instead.)
const BUILT_IN_KEY = process.env.NEXT_PUBLIC_RECOUP_API_KEY || "";
const SESSION_KEY = "recoup.api-key";

export function apiKey(): string {
  if (BUILT_IN_KEY) return BUILT_IN_KEY;
  try {
    return window.sessionStorage.getItem(SESSION_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setApiKey(key: string): void {
  try {
    window.sessionStorage.setItem(SESSION_KEY, key);
  } catch {
    /* private mode: the key lasts until the page reloads */
  }
}

/** The person the API key belongs to. The server takes the name from the key, not from here. */
export function currentUser(): string {
  return process.env.NEXT_PUBLIC_RECOUP_USER ?? "demo-biller";
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "X-API-Key": apiKey(),
      ...(init.body && !(init.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep the status text */
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const post = <T,>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });

export function pdfUrl(documentId: string): string {
  return `${BASE}/contracts/${documentId}/pdf`;
}

interface Loaded<T> {
  path: string;
  data: T | null;
  error: ApiError | Error | null;
}

/**
 * Fetch on mount and whenever `path` changes; `reload` refetches after a mutation.
 * Results are tagged with the path they belong to, so a page never shows (or acts on)
 * one record while its URL names another.
 */
export function useApi<T>(path: string | null) {
  const [loaded, setLoaded] = useState<Loaded<T> | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (path === null) return;
    let cancelled = false;
    api<T>(path)
      .then((value) => {
        if (!cancelled) setLoaded({ path, data: value, error: null });
      })
      .catch((reason: unknown) => {
        if (!cancelled) setLoaded({ path, data: null, error: reason instanceof Error ? reason : new Error(String(reason)) });
      });
    return () => {
      cancelled = true;
    };
  }, [path, version]);
  const reload = useCallback(() => setVersion((v) => v + 1), []);
  const current = loaded && loaded.path === path ? loaded : null;
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    reload,
    loading: path !== null && current === null,
  };
}

export function money(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  const number = typeof value === "number" ? value : Number(value);
  return number.toLocaleString("en-US", { style: "currency", currency: "USD" });
}
