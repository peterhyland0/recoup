"use client";

import { type ReactNode, useState, useSyncExternalStore } from "react";

import { apiKey, setApiKey } from "@/lib/api";

const subscribe = () => () => {};

/** Asks for an API key when the build has none. Renders nothing extra once a key is present. */
export function KeyGate({ children }: { children: ReactNode }) {
  const key = useSyncExternalStore(subscribe, apiKey, () => "pending");
  const [draft, setDraft] = useState("");
  if (key === "pending") return null;
  if (key) return <>{children}</>;
  return (
    <form
      className="section signin"
      onSubmit={(event) => {
        event.preventDefault();
        setApiKey(draft.trim());
        window.location.reload();
      }}
    >
      <h2>Sign in</h2>
      <p className="muted small">Paste your Recoup API key. It is kept in this browser tab only.</p>
      <div className="actions">
        <input
          aria-label="API key"
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button type="submit" disabled={!draft.trim()}>
          Continue
        </button>
      </div>
    </form>
  );
}
